import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.channels.telegram.service import TelegramService
from app.config import Settings, get_settings
from app.main import app
from tests.telegram_fakes import TOKEN, WEBHOOK_SECRET, FakeAnalyzer, FakeTelegram, text_update

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
URL = "/webhook/telegram"


def settings_with(**overrides) -> Settings:
    values = {"telegram_bot_token": TOKEN, "telegram_webhook_secret": WEBHOOK_SECRET} | overrides
    return Settings(_env_file=None, **values)


class Env:
    def __init__(self):
        self.tg = FakeTelegram()
        self.analyzer = FakeAnalyzer()
        self.service = TelegramService(self.tg.client(), analyze=self.analyzer)
        self.client = TestClient(app)  # no `with`: the lifespan is not run

    def post(self, payload, *, secret=WEBHOOK_SECRET, **kwargs):
        headers = {SECRET_HEADER: secret} if secret is not None else {}
        return self.client.post(URL, json=payload, headers=headers, **kwargs)

    @property
    def nothing_happened(self) -> bool:
        return self.analyzer.calls == [] and self.tg.calls == []


@pytest.fixture
def env():
    environment = Env()
    app.state.telegram_service = environment.service
    app.dependency_overrides[get_settings] = lambda: settings_with()
    yield environment
    app.dependency_overrides.clear()
    app.state.telegram_service = None


# --- authentication -------------------------------------------------------------------------------


def test_valid_secret_is_accepted_and_processed(env):
    response = env.post(text_update("Please share the OTP"))
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(env.analyzer.calls) == 1
    assert len(env.tg.sent_messages) == 1


def test_wrong_secret_gets_401_and_nothing_runs(env):
    response = env.post(text_update("hello"), secret="wrong-secret")
    assert response.status_code == 401
    assert env.nothing_happened


def test_missing_secret_header_gets_401(env):
    response = env.post(text_update("hello"), secret=None)
    assert response.status_code == 401
    assert env.nothing_happened


def test_empty_secret_header_gets_401(env):
    assert env.post(text_update("hello"), secret="").status_code == 401
    assert env.nothing_happened


def test_secret_that_is_only_a_prefix_is_rejected(env):
    assert env.post(text_update("hello"), secret=WEBHOOK_SECRET[:-1]).status_code == 401
    assert env.post(text_update("hello"), secret=WEBHOOK_SECRET + "x").status_code == 401


def test_no_configured_secret_rejects_everything(env):
    app.dependency_overrides[get_settings] = lambda: settings_with(telegram_webhook_secret=None)
    assert env.post(text_update("hello"), secret="anything").status_code == 401
    assert env.post(text_update("hello"), secret=None).status_code == 401
    assert env.nothing_happened


def test_secret_is_checked_before_the_body_is_read(env):
    response = env.client.post(URL, content=b"this is not json", headers={SECRET_HEADER: "wrong"})
    assert response.status_code == 401  # not 200 / 422


def test_401_response_does_not_leak_the_secret(env):
    response = env.post(text_update("hello"), secret="wrong")
    assert WEBHOOK_SECRET not in response.text and "wrong" not in response.text


def test_secret_comparison_is_constant_time(env, monkeypatch):
    import app.channels.telegram.webhook as webhook

    calls = []
    real = webhook.hmac.compare_digest
    monkeypatch.setattr(webhook.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    env.post(text_update("hello"))
    assert calls == [1]


def test_unconfigured_bot_returns_503_after_authentication(env):
    app.state.telegram_service = None
    assert env.post(text_update("hello")).status_code == 503
    assert env.post(text_update("hello"), secret="wrong").status_code == 401  # auth still comes first


# --- bodies we cannot use still get 200 (so Telegram does not retry) ---------------------------------------


def test_invalid_json_with_valid_secret_is_ignored(env):
    response = env.client.post(URL, content=b"not json at all", headers={SECRET_HEADER: WEBHOOK_SECRET})
    assert response.status_code == 200
    assert env.nothing_happened


@pytest.mark.parametrize(
    "payload",
    [
        {"message": {"text": "no update id"}},
        {"update_id": "not-a-number"},
        {"update_id": 1, "message": "a string, not an object"},
        {"update_id": 1, "message": {"message_id": 1}},  # no chat
        ["a", "list"],
        None,
    ],
)
def test_malformed_updates_are_ignored_without_logging_content(env, caplog, payload):
    with caplog.at_level(logging.DEBUG):
        response = env.client.post(URL, json=payload, headers={SECRET_HEADER: WEBHOOK_SECRET})
    assert response.status_code == 200
    assert env.nothing_happened
    for fragment in ("no update id", "not-a-number", "a string, not an object"):
        assert fragment not in caplog.text


def test_update_types_we_ignore_still_get_200(env):
    assert env.post({"update_id": 5, "edited_message": {"message_id": 1}}).status_code == 200
    assert env.post(text_update("hello", chat_type="group", update_id=6)).status_code == 200
    assert env.nothing_happened


# --- de-duplication ------------------------------------------------------------------------------------------------


def test_duplicate_update_is_acknowledged_but_processed_once(env):
    payload = text_update("Please share the OTP", update_id=42)
    assert env.post(payload).status_code == 200
    assert env.post(payload).status_code == 200  # Telegram redelivers
    assert env.post(payload).status_code == 200
    assert len(env.analyzer.calls) == 1
    assert len(env.tg.sent_messages) == 1


def test_different_update_ids_are_all_processed(env):
    for update_id in (1, 2, 3):
        env.post(text_update("hello", update_id=update_id))
    assert len(env.analyzer.calls) == 3


# --- 200 immediately, processing in the background -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_response_is_sent_before_background_processing_finishes(env):
    """Drive the ASGI app by hand. TestClient waits for background tasks, so it
    cannot show whether the 200 went out first; the raw `send` callback can."""
    events: list[str] = []
    release = asyncio.Event()

    class StubService:
        def accept(self, update):
            events.append("accepted")
            return True

        async def handle_update(self, update):
            events.append("task_started")
            await release.wait()  # processing is "slow"
            events.append("task_finished")

    app.state.telegram_service = StubService()
    body = json.dumps(text_update("hello")).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": URL, "raw_path": URL.encode(), "query_string": b"", "client": ("127.0.0.1", 1), "server": ("test", 80),
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (SECRET_HEADER.lower().encode(), WEBHOOK_SECRET.encode()),
        ],
    }  # fmt: skip
    sent: list[dict] = []
    response_complete = asyncio.Event()
    request_delivered = False

    async def receive():
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            events.append("response_sent")
            response_complete.set()

    app_call = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(response_complete.wait(), timeout=5)

    # The client already has its 200, while the work has not finished.
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
    assert json.loads(sent[1]["body"]) == {"ok": True}
    assert "task_finished" not in events
    assert events.index("response_sent") < events.index("task_started")

    release.set()
    await asyncio.wait_for(app_call, timeout=5)
    assert events == ["accepted", "response_sent", "task_started", "task_finished"]


# --- app wiring ------------------------------------------------------------------------------------------------------------


def test_health_still_works_and_needs_no_secret(env):
    response = env.client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_lifespan_builds_the_service_when_a_token_is_set(monkeypatch):
    monkeypatch.setattr(main_module, "settings", settings_with())
    with TestClient(app) as client:
        assert isinstance(app.state.telegram_service, TelegramService)
        assert client.get("/health").status_code == 200


def test_lifespan_without_a_token_disables_the_webhook(monkeypatch, caplog):
    monkeypatch.setattr(main_module, "settings", settings_with(telegram_bot_token=None))
    app.dependency_overrides[get_settings] = lambda: settings_with(telegram_bot_token=None)
    try:
        with caplog.at_level(logging.WARNING):
            with TestClient(app) as client:
                assert app.state.telegram_service is None
                assert client.get("/health").status_code == 200
                assert client.post(URL, json={}, headers={SECRET_HEADER: WEBHOOK_SECRET}).status_code == 503
    finally:
        app.dependency_overrides.clear()
    assert "TELEGRAM_BOT_TOKEN is not set" in caplog.text
