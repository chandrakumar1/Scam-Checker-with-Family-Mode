"""Family Mode wiring: the runtime builder, the app lifespan, and the scripts.
Network access is blocked by conftest; Telegram is a MockTransport fake."""

import logging
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import scripts.poll as poll
import scripts.set_webhook as set_webhook
from app.channels.telegram.family_handler import FamilyTelegramHandler
from app.config import Settings, get_settings
from app.db.engine import init_db, make_engine
from app.family.service import AsyncFamilyService, FamilyService
from app.family.types import AlertSummary, AlertVerdict, PersonRef, ScamCategory, SourceKind
from app.main import app
from app.replies.formatter import help_message
from app.wiring import TelegramRuntime, _warn_if_sqlite_in_production, build_family_policy, build_telegram_runtime
from tests.family_helpers import Clock, all_alerts, all_links, link_pair
from tests.telegram_fakes import TOKEN, WEBHOOK_SECRET, FakeTelegram, api_error, text_update

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
FAMILY_UPDATES = ["message", "callback_query", "my_chat_member"]


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = {
        "telegram_bot_token": TOKEN,
        "telegram_webhook_secret": WEBHOOK_SECRET,
        "database_url": f"sqlite:///{tmp_path / 'family.db'}",
    }
    return Settings(_env_file=None, **(base | overrides))


def db_file(tmp_path: Path) -> Path:
    return tmp_path / "family.db"


# --- policy from settings --------------------------------------------------------------------------------------------


def test_the_policy_is_built_from_the_settings(tmp_path):
    policy = build_family_policy(
        make_settings(
            tmp_path, family_invite_ttl_hours=12, family_consent_ttl_hours=6, family_max_helpers=2,
            family_max_pending_invites=1, family_alert_max_per_hour=9, family_alert_dedupe_minutes=10,
            family_alert_retention_days=7, emergency_dedupe_seconds=60,
        )
    )  # fmt: skip
    assert policy.invite_ttl == timedelta(hours=12) and policy.consent_ttl == timedelta(hours=6)
    assert (policy.max_helpers, policy.max_pending_invites, policy.alert_max_per_hour) == (2, 1, 9)
    assert policy.alert_dedupe_window == timedelta(minutes=10) and policy.alert_retention == timedelta(days=7)
    assert policy.emergency_dedupe_window == timedelta(seconds=60)


def test_the_default_settings_give_the_approved_default_policy(tmp_path):
    from app.family.policy import DEFAULT_POLICY

    assert build_family_policy(make_settings(tmp_path)) == DEFAULT_POLICY


# --- the runtime builder ---------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_with_family_mode_off_nothing_is_opened_or_created(tmp_path):
    tg = FakeTelegram()
    runtime = await build_telegram_runtime(make_settings(tmp_path), tg.client())
    assert isinstance(runtime, TelegramRuntime)
    assert runtime.service._family is None and runtime.engine is None and not runtime.family_enabled
    assert not db_file(tmp_path).exists()
    assert list(tmp_path.iterdir()) == []  # not even an empty file
    runtime.close()
    runtime.close()  # safe to call twice


@pytest.mark.anyio
async def test_with_family_mode_on_the_database_and_handler_are_set_up(tmp_path):
    tg = FakeTelegram()
    settings = make_settings(
        tmp_path, family_mode_enabled=True, telegram_bot_username="ruko_dev_bot", family_invite_ttl_hours=12,
        family_max_helpers=2, family_max_pending_invites=1,
    )  # fmt: skip
    runtime = await build_telegram_runtime(settings, tg.client())
    try:
        assert runtime.family_enabled and runtime.engine is not None
        handler = runtime.service._family
        assert isinstance(handler, FamilyTelegramHandler)
        assert (handler._bot_username_cached, handler._invite_ttl_hours) == ("ruko_dev_bot", 12)
        assert (handler._max_helpers, handler._max_pending) == (2, 1)
        assert db_file(tmp_path).exists() and stat.S_IMODE(os.stat(db_file(tmp_path)).st_mode) == 0o600
        from sqlalchemy import inspect

        assert set(inspect(runtime.engine).get_table_names()) == {"person", "family_link", "family_alert", "family_alert_delivery"}
    finally:
        runtime.close()
    assert runtime.engine is None


@pytest.mark.anyio
async def test_the_service_in_both_modes_shares_the_same_other_settings(tmp_path):
    tg = FakeTelegram()
    off = await build_telegram_runtime(make_settings(tmp_path, telegram_max_concurrent_analyses=3), tg.client())
    on = await build_telegram_runtime(
        make_settings(tmp_path, telegram_max_concurrent_analyses=3, family_mode_enabled=True), tg.client()
    )
    try:
        assert off.service._semaphore._value == on.service._semaphore._value == 3
    finally:
        off.close()
        on.close()


@pytest.mark.anyio
async def test_startup_housekeeping_expires_old_invites_and_purges_old_alerts(tmp_path, caplog):
    engine = make_engine(f"sqlite:///{db_file(tmp_path)}")
    init_db(engine)
    long_ago = Clock(datetime(2020, 1, 1, tzinfo=UTC))
    family = FamilyService(engine, enabled=True, clock=long_ago)
    link_pair(family, PersonRef(1, "P"), PersonRef(2, "G"))
    family.request_normal_alert(PersonRef(1, "P"), AlertSummary(AlertVerdict.LIKELY_SCAM, ScamCategory.REFUND, SourceKind.TEXT), 1)
    family.create_invite(PersonRef(1, "P"))  # an invitation nobody opened, from 2020
    assert len(all_alerts(engine)) == 1
    engine.dispose()

    caplog.set_level(logging.INFO)
    runtime = await build_telegram_runtime(make_settings(tmp_path, family_mode_enabled=True), FakeTelegram().client())
    try:
        assert all_alerts(runtime.engine) == []  # the 2020 alert is older than the retention period
        assert sorted(link.status for link in all_links(runtime.engine)) == ["active", "expired"]
        assert "cleaned up 1 expired links and 1 old alerts" in caplog.text
    finally:
        runtime.close()


@pytest.mark.anyio
async def test_if_housekeeping_fails_family_mode_still_starts(tmp_path, caplog, monkeypatch):
    async def boom(self):
        raise RuntimeError("housekeeping trouble")

    monkeypatch.setattr(AsyncFamilyService, "maintenance", boom)
    caplog.set_level(logging.WARNING)
    runtime = await build_telegram_runtime(make_settings(tmp_path, family_mode_enabled=True), FakeTelegram().client())
    try:
        assert runtime.family_enabled and runtime.service._family is not None
        assert "housekeeping failed (RuntimeError)" in caplog.text and "trouble" not in caplog.text
    finally:
        runtime.close()


# --- Family Mode must never be able to stop the bot from checking scams ---------------------------------------------------------


@pytest.mark.anyio
async def test_an_unusable_database_means_no_family_mode_but_a_working_bot(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    bad_url = "sqlite:////nonexistent_dir_for_ruko_tests/deep/ruko.db"
    runtime = await build_telegram_runtime(make_settings(tmp_path, family_mode_enabled=True, database_url=bad_url), FakeTelegram().client())
    assert runtime.service is not None and runtime.service._family is None
    assert runtime.engine is None and not runtime.family_enabled
    assert "Family Mode could not start (OperationalError); continuing without it" in caplog.text
    assert "nonexistent_dir_for_ruko_tests" not in caplog.text  # the path (like a URL) is never logged


@pytest.mark.anyio
async def test_a_database_url_with_a_password_is_never_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    url = "postgresql://ruko_user:SuperSecretPassw0rd@db.example.internal/ruko"
    runtime = await build_telegram_runtime(make_settings(tmp_path, family_mode_enabled=True, database_url=url), FakeTelegram().client())
    assert runtime.service._family is None  # no driver / no connection: it falls back safely
    for fragment in ("SuperSecretPassw0rd", "ruko_user", "db.example.internal"):
        assert fragment not in caplog.text


@pytest.mark.anyio
async def test_a_failed_start_still_serves_ordinary_messages(tmp_path):
    from tests.telegram_fakes import FakeAnalyzer, parse

    tg = FakeTelegram()
    bad_url = "sqlite:////nonexistent_dir_for_ruko_tests/ruko.db"
    runtime = await build_telegram_runtime(make_settings(tmp_path, family_mode_enabled=True, database_url=bad_url), tg.client())
    runtime.service._analyze = FakeAnalyzer()
    await runtime.service.handle_update(parse(text_update("/family")))
    assert tg.sent_texts == [help_message("en")]  # exactly Phase 2: an unknown command


# --- the production-persistence warning --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environment", "url", "warns"),
    [
        ("production", "sqlite:///./ruko.db", True),
        ("PRODUCTION", "sqlite:///./ruko.db", True),
        (" production ", "SQLite:///./ruko.db", True),
        ("development", "sqlite:///./ruko.db", False),
        ("staging", "sqlite:///./ruko.db", False),
        ("production", "postgresql://u:p@h/db", False),
    ],
)
def test_sqlite_in_production_is_flagged(tmp_path, caplog, environment, url, warns):
    caplog.set_level(logging.WARNING)
    _warn_if_sqlite_in_production(make_settings(tmp_path, environment=environment, database_url=url))
    assert ("persistent storage" in caplog.text) is warns


@pytest.mark.anyio
async def test_the_warning_appears_only_when_family_mode_is_on(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    off = await build_telegram_runtime(make_settings(tmp_path, environment="production"), FakeTelegram().client())
    assert "persistent storage" not in caplog.text
    on = await build_telegram_runtime(make_settings(tmp_path, environment="production", family_mode_enabled=True), FakeTelegram().client())
    try:
        assert "persistent storage" in caplog.text
        assert str(tmp_path) not in caplog.text and "ruko.db" not in caplog.text
    finally:
        off.close()
        on.close()


# --- the app lifespan ------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def fake_http(monkeypatch):
    """Make the app's HTTP client talk to FakeTelegram instead of the internet."""
    tg = FakeTelegram()
    monkeypatch.setattr(main_module, "make_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(tg._handler)))
    return tg


def run_app(monkeypatch, settings):
    monkeypatch.setattr(main_module, "settings", settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_app_overrides():
    yield
    app.dependency_overrides.clear()
    app.state.telegram_service = None


def test_lifespan_with_family_mode_off_has_no_family_but_keeps_emergency(tmp_path, monkeypatch, fake_http):
    with run_app(monkeypatch, make_settings(tmp_path)) as client:
        service = app.state.telegram_service
        assert service._family is None and service._alerts is None  # no family consent flow, no family alerts
        assert service._emergency is not None  # Emergency is always on...
        assert service._emergency._alerts is None  # ...but with no family side to alert
        assert service._client._allowed_updates == ["message", "callback_query"]  # buttons yes, block events no
        assert client.get("/health").json() == {"status": "ok"}
    assert not db_file(tmp_path).exists()


def test_lifespan_with_family_mode_on_wires_the_handler_and_the_wider_update_types(tmp_path, monkeypatch, fake_http):
    with run_app(monkeypatch, make_settings(tmp_path, family_mode_enabled=True, telegram_bot_username="ruko_dev_bot")):
        service = app.state.telegram_service
        assert isinstance(service._family, FamilyTelegramHandler)
        assert service._client._allowed_updates == FAMILY_UPDATES
    assert db_file(tmp_path).exists()


def test_lifespan_with_family_mode_on_but_no_bot_token_stays_inactive(tmp_path, monkeypatch, fake_http, caplog):
    caplog.set_level(logging.WARNING)
    with run_app(monkeypatch, make_settings(tmp_path, family_mode_enabled=True, telegram_bot_token=None)) as client:
        assert app.state.telegram_service is None
        assert client.get("/health").status_code == 200
    assert "FAMILY_MODE_ENABLED is on but TELEGRAM_BOT_TOKEN is not set" in caplog.text
    assert not db_file(tmp_path).exists()


def test_lifespan_survives_a_broken_database(tmp_path, monkeypatch, fake_http, caplog):
    caplog.set_level(logging.WARNING)
    settings = make_settings(tmp_path, family_mode_enabled=True, database_url="sqlite:////nonexistent_dir_for_ruko_tests/x.db")
    with run_app(monkeypatch, settings) as client:
        assert app.state.telegram_service is not None and app.state.telegram_service._family is None
        assert client.get("/health").status_code == 200
    assert "continuing without it" in caplog.text


def test_a_family_command_goes_through_the_webhook_to_a_reply_and_the_database(tmp_path, monkeypatch, fake_http):
    settings = make_settings(tmp_path, family_mode_enabled=True, telegram_bot_username="ruko_dev_bot")
    with run_app(monkeypatch, settings) as client:
        response = client.post("/webhook/telegram", json=text_update("/family"), headers={SECRET_HEADER: WEBHOOK_SECRET})
        assert response.status_code == 200
        [menu] = fake_http.sent_messages
        assert "Family helpers" in menu["text"]
        assert menu["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "fam:add"
    engine = make_engine(f"sqlite:///{db_file(tmp_path)}")
    try:
        from tests.family_helpers import all_people

        assert [p.telegram_user_id for p in all_people(engine)] == [100]
    finally:
        engine.dispose()


def test_a_button_press_goes_through_the_webhook_and_is_acknowledged(tmp_path, monkeypatch, fake_http):
    settings = make_settings(tmp_path, family_mode_enabled=True, telegram_bot_username="ruko_dev_bot")
    press = {
        "update_id": 7,
        "callback_query": {
            "id": "cbq-7", "from": {"id": 100, "is_bot": False, "first_name": "Test", "language_code": "en"},
            "message": {"message_id": 5, "date": 1, "chat": {"id": 100, "type": "private"}}, "data": "fam:add",
        },
    }  # fmt: skip
    with run_app(monkeypatch, settings) as client:
        assert client.post("/webhook/telegram", json=press, headers={SECRET_HEADER: WEBHOOK_SECRET}).status_code == 200
        assert [c["callback_query_id"] for c in fake_http.method_calls("answerCallbackQuery")] == ["cbq-7"]
        assert "Before I make an invitation link" in fake_http.sent_messages[0]["text"]


def test_without_family_mode_a_button_press_through_the_webhook_is_ignored(tmp_path, monkeypatch, fake_http):
    press = {
        "update_id": 7,
        "callback_query": {"id": "cbq-7", "from": {"id": 100, "is_bot": False}, "message": {"message_id": 5, "chat": {"id": 100, "type": "private"}}, "data": "fam:add"},
    }  # fmt: skip
    with run_app(monkeypatch, make_settings(tmp_path)) as client:
        assert client.post("/webhook/telegram", json=press, headers={SECRET_HEADER: WEBHOOK_SECRET}).status_code == 200
        assert fake_http.calls == []


def test_the_webhook_still_rejects_a_wrong_secret_with_family_mode_on(tmp_path, monkeypatch, fake_http):
    settings = make_settings(tmp_path, family_mode_enabled=True, telegram_bot_username="ruko_dev_bot")
    with run_app(monkeypatch, settings) as client:
        assert client.post("/webhook/telegram", json=text_update("/family"), headers={SECRET_HEADER: "wrong"}).status_code == 401
        assert fake_http.calls == []


# --- the scripts --------------------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def fake_script_http(monkeypatch):
    tg = FakeTelegram()
    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(tg._handler))  # noqa: E731
    monkeypatch.setattr(poll, "make_http_client", factory)
    monkeypatch.setattr(set_webhook, "make_http_client", factory)
    return tg


@pytest.mark.anyio
async def test_poll_asks_for_the_family_update_types_and_wires_the_handler_when_on(tmp_path, fake_script_http, capsys):
    tg = fake_script_http
    tg.queue("getUpdates", httpx.Response(200, json={"ok": True, "result": [text_update("/family", update_id=1)]}), api_error(409, "Conflict"))
    code = await poll.run(make_settings(tmp_path, family_mode_enabled=True, telegram_bot_username="ruko_dev_bot"))
    assert code == 1  # ended by the scripted 409
    assert all(call["allowed_updates"] == FAMILY_UPDATES for call in tg.method_calls("getUpdates"))
    assert "Family helpers" in tg.sent_messages[0]["text"]  # the family handler really is attached
    assert db_file(tmp_path).exists()


@pytest.mark.anyio
async def test_poll_without_family_mode_asks_only_for_messages_and_buttons(tmp_path, fake_script_http):
    tg = fake_script_http
    tg.queue("getUpdates", httpx.Response(200, json={"ok": True, "result": [text_update("/family", update_id=1)]}), api_error(409, "Conflict"))
    assert await poll.run(make_settings(tmp_path)) == 1
    assert all(call["allowed_updates"] == ["message", "callback_query"] for call in tg.method_calls("getUpdates"))
    assert tg.sent_texts == [help_message("en")]
    assert not db_file(tmp_path).exists()


@pytest.mark.anyio
@pytest.mark.parametrize(("on", "expected"), [(True, FAMILY_UPDATES), (False, ["message", "callback_query"])])
async def test_set_webhook_registers_the_right_update_types(tmp_path, fake_script_http, monkeypatch, on, expected):
    import argparse

    settings = make_settings(tmp_path, public_base_url="https://ruko.example.com", family_mode_enabled=on)
    monkeypatch.setattr(set_webhook, "get_settings", lambda: settings)
    code = await set_webhook._main(argparse.Namespace(delete=False, info=False, drop_pending=False))
    assert code == 0
    assert fake_script_http.method_calls("setWebhook")[0]["allowed_updates"] == expected


# --- Emergency is always built, whatever Family Mode says ---------------------------------------------------------------


@pytest.mark.anyio
async def test_emergency_and_alerts_share_the_family_side_when_family_mode_is_on(tmp_path):
    runtime = await build_telegram_runtime(make_settings(tmp_path, family_mode_enabled=True), FakeTelegram().client())
    try:
        service = runtime.service
        assert service._emergency is not None and service._alerts is not None
        assert service._emergency._alerts is service._alerts  # one sender, so one delivery and re-check path for both kinds
    finally:
        runtime.close()


@pytest.mark.anyio
async def test_emergency_survives_a_family_mode_that_could_not_start(tmp_path):
    from app.replies.emergency_formatter import emergency_message, guidance_message
    from tests.telegram_fakes import parse

    bad_url = "sqlite:////nonexistent_dir_for_ruko_tests/ruko.db"
    tg = FakeTelegram()
    runtime = await build_telegram_runtime(make_settings(tmp_path, family_mode_enabled=True, database_url=bad_url), tg.client())
    service = runtime.service
    assert service._family is None and service._alerts is None and service._emergency is not None

    await service.handle_update(parse(text_update("/emergency")))
    yes = tg.sent_messages[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    press = {
        "update_id": 9,
        "callback_query": {
            "id": "c1", "from": {"id": 100, "is_bot": False, "first_name": "T"},
            "message": {"message_id": 5, "date": 1, "chat": {"id": 100, "type": "private"}}, "data": yes,
        },
    }  # fmt: skip
    await service.handle_update(parse(press))
    assert tg.sent_texts[-2:] == [guidance_message("en"), emergency_message("family_off", "en")]


@pytest.mark.anyio
async def test_the_button_lifetime_comes_from_the_settings(tmp_path):
    runtime = await build_telegram_runtime(make_settings(tmp_path, emergency_button_ttl_hours=2), FakeTelegram().client())
    assert runtime.service._emergency._codec.ttl == timedelta(hours=2)
    default = await build_telegram_runtime(make_settings(tmp_path), FakeTelegram().client())
    assert default.service._emergency._codec.ttl == timedelta(hours=24)


def test_a_slash_emergency_goes_through_the_webhook_with_family_mode_off(tmp_path, monkeypatch, fake_http):
    with run_app(monkeypatch, make_settings(tmp_path)) as client:
        response = client.post("/webhook/telegram", json=text_update("/emergency"), headers={SECRET_HEADER: WEBHOOK_SECRET})
        assert response.status_code == 200
        [question] = fake_http.sent_messages
        assert question["text"].startswith("Did you already send money, OTP, PIN, password, or card details?")

        yes = question["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        press = {
            "update_id": 5,
            "callback_query": {
                "id": "c1", "from": {"id": 100, "is_bot": False, "first_name": "T"},
                "message": {"message_id": 5, "date": 1, "chat": {"id": 100, "type": "private"}}, "data": yes,
            },
        }  # fmt: skip
        response = client.post("/webhook/telegram", json=press, headers={SECRET_HEADER: WEBHOOK_SECRET})
        assert response.status_code == 200
        assert fake_http.sent_texts[1].startswith("🆘 ACT NOW")
        assert "call someone you trust now" in fake_http.sent_texts[-1].lower()
        assert [c["callback_query_id"] for c in fake_http.method_calls("answerCallbackQuery")] == ["c1"]
    assert not db_file(tmp_path).exists()  # with Family Mode off, an emergency never opens a database
