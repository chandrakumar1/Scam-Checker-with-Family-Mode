import httpx
import pytest

from app.config import Settings
from scripts import set_webhook
from scripts.set_webhook import WEBHOOK_PATH, build_webhook_url, run
from tests.telegram_fakes import TOKEN, WEBHOOK_SECRET, FakeTelegram, api_error

pytestmark = pytest.mark.anyio


def settings(**overrides) -> Settings:
    values = {
        "telegram_bot_token": TOKEN,
        "telegram_webhook_secret": WEBHOOK_SECRET,
        "public_base_url": "https://ruko.example.com",
    } | overrides
    return Settings(_env_file=None, **values)


@pytest.fixture
def tg():
    return FakeTelegram()


# --- URL building ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://ruko.example.com", "https://ruko.example.com/webhook/telegram"),
        ("https://ruko.example.com/", "https://ruko.example.com/webhook/telegram"),
        ("  https://ruko.example.com  ", "https://ruko.example.com/webhook/telegram"),
        ("https://example.com/ruko", "https://example.com/ruko/webhook/telegram"),
        ("https://abc123.ngrok.app", "https://abc123.ngrok.app/webhook/telegram"),
    ],
)
def test_build_webhook_url(base, expected):
    assert build_webhook_url(base) == expected
    assert expected.endswith(WEBHOOK_PATH)


@pytest.mark.parametrize(
    "bad",
    [None, "", "   ", "http://ruko.example.com", "ruko.example.com", "https://", "ftp://x.com",
     "https://ruko.example.com?x=1", "https://ruko.example.com/#frag"],
)  # fmt: skip
def test_build_webhook_url_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        build_webhook_url(bad)


# --- running -------------------------------------------------------------------------------------------------


async def test_set_webhook_registers_url_secret_and_allowed_updates(tg, capsys):
    assert await run(tg.client(), settings()) == 0
    assert tg.method_calls("setWebhook") == [
        {
            "url": "https://ruko.example.com/webhook/telegram",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message"],
            "drop_pending_updates": False,
        }
    ]
    out = capsys.readouterr().out
    assert "https://ruko.example.com/webhook/telegram" in out


async def test_drop_pending_flag(tg):
    await run(tg.client(), settings(), drop_pending=True)
    assert tg.method_calls("setWebhook")[0]["drop_pending_updates"] is True


async def test_secrets_are_never_printed(tg, capsys):
    await run(tg.client(), settings())
    await run(tg.client(), settings(), info=True)
    captured = capsys.readouterr()
    for secret in (TOKEN, WEBHOOK_SECRET):
        assert secret not in captured.out + captured.err


async def test_refuses_to_register_without_a_secret(tg, capsys):
    assert await run(tg.client(), settings(telegram_webhook_secret=None)) == 1
    assert tg.method_calls("setWebhook") == []
    assert "TELEGRAM_WEBHOOK_SECRET" in capsys.readouterr().err


@pytest.mark.parametrize("base", [None, "http://insecure.example.com"])
async def test_refuses_a_missing_or_non_https_url(tg, capsys, base):
    assert await run(tg.client(), settings(public_base_url=base)) == 1
    assert tg.method_calls("setWebhook") == []
    assert "PUBLIC_BASE_URL" in capsys.readouterr().err


async def test_info_shows_status(tg, capsys):
    tg.webhook_url = "https://ruko.example.com/webhook/telegram"
    tg.queue(
        "getWebhookInfo",
        httpx.Response(200, json={"ok": True, "result": {
            "url": tg.webhook_url, "pending_update_count": 3, "last_error_message": "Wrong response: 401",
        }}),
    )  # fmt: skip
    assert await run(tg.client(), settings(), info=True) == 0
    out = capsys.readouterr().out
    assert "pending_update_count: 3" in out and "Wrong response: 401" in out
    assert tg.method_calls("setWebhook") == []


async def test_delete(tg, capsys):
    tg.webhook_url = "https://ruko.example.com/webhook/telegram"
    assert await run(tg.client(), settings(), delete=True, drop_pending=True) == 0
    assert tg.method_calls("deleteWebhook") == [{"drop_pending_updates": True}]
    assert "Webhook deleted" in capsys.readouterr().out


async def test_telegram_errors_are_reported_without_the_token(tg, capsys):
    tg.queue("setWebhook", api_error(400, "Bad Request: bad webhook: HTTPS url must be provided for webhook"))
    assert await run(tg.client(), settings()) == 1
    err = capsys.readouterr().err
    assert "setWebhook" in err and "400" in err
    assert TOKEN not in err


def test_delete_and_info_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as excinfo:
        set_webhook.main(["--delete", "--info"])
    assert excinfo.value.code == 2


async def test_main_without_a_token_exits_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(set_webhook, "get_settings", lambda: settings(telegram_bot_token=None))
    import argparse

    args = argparse.Namespace(delete=False, info=False, drop_pending=False)
    assert await set_webhook._main(args) == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
