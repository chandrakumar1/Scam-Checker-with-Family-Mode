# Decisions (Phases 1-3)

Locked decisions. Change one only on purpose, and update this file when you do.
Numbers below are the defaults in `app/config.py`.

## Phase 1: Analyzer

**Cautious verdicts.**
- Decision: only `likely_scam`, `suspicious`, `no_obvious_red_flags`; a message is never called "safe"; when unsure, take the more cautious one.
- Reason: no check can prove a message safe, and false reassurance is the worst failure for the people this bot protects.

**Rules and LLM combination.**
- Decision: rules are deterministic and can only raise the verdict (CRITICAL => at least `likely_scam`; HIGH/MEDIUM => at least `suspicious`). The LLM can add caution, never remove it.
- Reason: a known scam pattern must not be talked down by a model that can be fooled or injected.

**LLM failure.**
- Decision: timeout, API error, refusal or invalid output counts as `suspicious`. A CRITICAL rule plus LLM failure stays `likely_scam`.
- Reason: a failed second opinion must never read as reassurance, and the rule floor still applies.

**15-second overall deadline.**
- Decision: `llm_timeout_seconds` (15 s) is one wall-clock deadline (`asyncio.timeout`) covering connect, upload, processing and the server-side refusal fallback; SDK retries are off.
- Reason: SDK timeouts are per step, so without a total budget a slow provider could hold a reply for far longer.

**Message content is data.**
- Decision: forwarded text/images are wrapped as untrusted data; the system prompt is fixed.
- Reason: scam messages can contain prompt-injection text.

## Phase 2: Telegram channel

**Telegram-first MVP; no WhatsApp or voice.**
- Decision: one channel (Telegram Bot API over httpx, webhook in production, `scripts/poll.py` for dev). WhatsApp and voice are out of the MVP.
- Reason: one channel keeps the MVP small; analyzer and replies stay channel-agnostic so others can be added later.

**In-memory update deduplication.**
- Decision: a per-process TTL set of seen `update_id`s.
- Reason: Phase 2 needed no database. A restart or second worker can re-process an update; Family alerts have their own database idempotency key.

**No per-chat rate limiter yet.**
- Decision: only a concurrency cap (5 analyses at once) exists.
- Reason: deferred to hardening. It is a production blocker for real-family rollout (see `README.md`).

**English and Hindi.**
- Decision: replies in English or Hindi, chosen automatically from the message script or Telegram `language_code`; nothing is stored.
- Reason: covers the intended first users without a settings flow.

**Images over 5 MiB are rejected,** with no silent downgrade to a smaller variant. Reason: bounded memory and cost; analysing a different image than the one sent would be misleading.

## Phase 3: Family Mode and Emergency

**Normal Family alerts are `likely_scam` only.**
- Decision: `suspicious` never creates a normal alert. Enforced in the Telegram layer and again in `FamilyService`.
- Reason: helpers should hear only about strong cases; the person still gets their own reply and the Emergency button.

**No original content in Family Mode.**
- Decision: only `AlertSummary` (verdict, scam type, source kind; enums only) plus identity and operation ids cross into `app/family`; no raw LLM label; the database stores no message or image content.
- Reason: helpers must not receive a person's private messages, and a type of three enums can be checked by test.

**Three-step family consent.**
- Decision: a link goes invited => awaiting_guardian => awaiting_protected => active, and is active only once the steps are done. Either person can leave; `/deleteme` removes the person's data.
- Reason: nobody becomes a helper, or is watched, without explicit consent.

**Limits.**
- Decision: 5 helpers per protected person; 3 pending invites; invitations expire (48 h) and consents expire (24 h).
- Reason: bounds spam and abuse of the invite flow.

**Normal alert limits.**
- Decision: at most 5 normal alerts per hour per person; a second alert of the same scam category within 30 minutes is suppressed.
- Reason: stops floods and repeats reaching helpers.

**Emergency alerts.**
- Decision: they bypass the hourly limit, with one per person per 2 minutes; a failed delivery still counts toward that window.
- Reason: a person in real trouble must get through, while repeated taps must not spam helpers.

**24-hour emergency button expiry.**
- Decision: buttons carry a signed stateless token (owner, signature, issue time); presses older than 24 h are refused.
- Reason: needs no storage, and stale or forwarded buttons cannot act forever.

**Emergency is independent of Family Mode.**
- Decision: `/emergency` and the button work with `FAMILY_MODE_ENABLED=false`, with no helpers linked, or if Family startup fails.
- Reason: a safety feature must not depend on an optional one.

**1930-first guidance.**
- Decision: Yes sends the static 1930 steps before any Family or database work, then an explicit outcome message.
- Reason: the most important help must not wait on, or be lost to, a database or network problem.

**Delivery re-check.**
- Decision: authorisation is re-read immediately before each send; if it fails or cannot be read, nothing is sent.
- Reason: a helper who left or blocked the bot a moment ago must not receive the alert.

**SQLite with `create_all`, for Phase 3 only.**
- Decision: SQLite via `DATABASE_URL`, tables made by `create_all`, no migrations. The schema stays portable (Postgres is the likely target). Production persistence is NOT decided.
- Reason: simplest while the schema settles. SQLite file storage has not been judged production-ready (see `README.md`).
