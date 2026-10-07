# Ruko

A scam-checking Telegram bot with Family Mode. Phases 0-3 are complete; the project is not deployed and is not production-ready.

- Working rules: `CLAUDE.md`
- Locked decisions: `docs/DECISIONS.md`
- Settings: `.env.example` (Family Mode is off by default: `FAMILY_MODE_ENABLED=false`)

## Production blockers

Nothing here is production-ready until these are done:

- [ ] **Persistent production database decision.** Family Mode uses SQLite with `create_all` and no migrations; a plain file has not been treated as production-ready.
- [ ] **Real Telegram smoke test with the dev bot.** All tests so far use fake Telegram.
- [ ] **Real LLM smoke test.** All tests so far use a fake LLM client.
- [ ] **Hindi native-speaker review.** The Hindi copy is a draft.
- [ ] **Re-run `python -m scripts.set_webhook` after deployment.** Allowed updates now include button presses (`callback_query`), even with Family Mode off.
- [ ] **Rate limiter before a real-family rollout.** Only a concurrency cap exists.
