import asyncio

import httpx
import pytest

from scripts import poll
from scripts.poll import PollingError, poll_loop, prepare
from app.config import Settings
from tests.telegram_fakes import TOKEN, FakeTelegram, api_error, api_ok, text_update

pytestmark = pytest.mark.anyio


class StubService:
    def __init__(self, duplicates=(), handle_delay: float = 0.0):
        self.duplicates = set(duplicates)
        self.handle_delay = handle_delay
        self.accepted: list[int] = []
        self.handled: list[int] = []

    def accept(self, update):
        if update.update_id in self.duplicates:
            return False
        self.accepted.append(update.update_id)
        return True

    async def handle_update(self, update):
        await asyncio.sleep(self.handle_delay)
        self.handled.append(update.update_id)


def stop_when_polled(stop: asyncio.Event):
    """A getUpdates response that ends the poll loop."""

    def responder(request):
        stop.set()
        return api_ok([])

    return responder


@pytest.fixture
def tg():
    return FakeTelegram()


async def run_loop(tg, service, *, stop=None, poll_timeout=30):
    stop = stop or asyncio.Event()
    await asyncio.wait_for(
        poll_loop(tg.client(), service, stop=stop, poll_timeout=poll_timeout, sleep=tg._sleep), timeout=10
    )


# --- offsets and dispatch ---------------------------------------------------------------------------------


async def test_offset_advances_past_the_highest_update_id(tg):
    stop = asyncio.Event()
    tg.queue(
        "getUpdates",
        api_ok([text_update("a", update_id=5), text_update("b", update_id=7)]),
        api_ok([text_update("c", update_id=8)]),
        stop_when_polled(stop),
    )
    service = StubService()
    await run_loop(tg, service, stop=stop)

    calls = tg.method_calls("getUpdates")
    assert "offset" not in calls[0]
    assert calls[1]["offset"] == 8
    assert calls[2]["offset"] == 9
    assert all(call["allowed_updates"] == ["message"] and call["timeout"] == 30 for call in calls)
    assert sorted(service.handled) == [5, 7, 8]


async def test_each_update_is_handled_as_its_own_task(tg):
    stop = asyncio.Event()
    tg.queue("getUpdates", api_ok([text_update("a", update_id=i) for i in (1, 2, 3)]), stop_when_polled(stop))
    service = StubService(handle_delay=0.2)
    started = asyncio.get_running_loop().time()
    await run_loop(tg, service, stop=stop)
    # Three 0.2s handlers ran concurrently, not one after another.
    assert asyncio.get_running_loop().time() - started < 0.5
    assert sorted(service.handled) == [1, 2, 3]


async def test_duplicates_are_not_handled_but_offset_still_advances(tg):
    stop = asyncio.Event()
    tg.queue("getUpdates", api_ok([text_update("a", update_id=1), text_update("a", update_id=2)]), stop_when_polled(stop))
    service = StubService(duplicates={1})
    await run_loop(tg, service, stop=stop)
    assert service.handled == [2]
    assert tg.method_calls("getUpdates")[1]["offset"] == 3


async def test_malformed_update_is_skipped_but_not_re_fetched(tg, caplog):
    stop = asyncio.Event()
    bad = {"update_id": 10, "message": "garbage-secret-text"}
    tg.queue("getUpdates", api_ok([bad, text_update("ok", update_id=11)]), stop_when_polled(stop))
    service = StubService()
    await run_loop(tg, service, stop=stop)
    assert service.handled == [11]
    assert tg.method_calls("getUpdates")[1]["offset"] == 12
    assert "garbage-secret-text" not in caplog.text


async def test_in_flight_handlers_finish_before_the_loop_returns(tg):
    stop = asyncio.Event()
    tg.queue("getUpdates", api_ok([text_update("a", update_id=1)]), stop_when_polled(stop))
    service = StubService(handle_delay=0.3)
    await run_loop(tg, service, stop=stop)
    assert service.handled == [1]


async def test_custom_poll_timeout_is_used(tg):
    stop = asyncio.Event()
    tg.queue("getUpdates", stop_when_polled(stop))
    await run_loop(tg, StubService(), stop=stop, poll_timeout=7)
    assert tg.method_calls("getUpdates")[0]["timeout"] == 7


# --- errors -----------------------------------------------------------------------------------------------------


async def test_bad_token_is_fatal(tg):
    tg.queue("getUpdates", api_error(401, "Unauthorized"))
    with pytest.raises(PollingError, match="401"):
        await run_loop(tg, StubService())


async def test_conflict_explains_the_webhook_problem(tg):
    tg.queue("getUpdates", api_error(409, "Conflict: can't use getUpdates method while webhook is active"))
    with pytest.raises(PollingError, match="(?i)webhook") as excinfo:
        await run_loop(tg, StubService())
    assert "--delete-webhook" in str(excinfo.value)


async def test_network_errors_back_off_and_recover(tg):
    stop = asyncio.Event()

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    tg.queue("getUpdates", boom, boom, boom, api_ok([text_update("a", update_id=1)]), boom, stop_when_polled(stop))
    service = StubService()
    await run_loop(tg, service, stop=stop)
    # 1s, 2s, 4s while down; reset to 1s after a success.
    assert tg.sleeps == [1.0, 2.0, 4.0, 1.0]
    assert service.handled == [1]


async def test_backoff_is_capped(tg):
    stop = asyncio.Event()

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    tg.queue("getUpdates", *[boom] * 8, stop_when_polled(stop))
    await run_loop(tg, StubService(), stop=stop)
    assert tg.sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


async def test_rate_limit_waits_for_retry_after(tg):
    stop = asyncio.Event()
    tg.queue("getUpdates", api_error(429, "Too Many Requests", retry_after=12), stop_when_polled(stop))
    await run_loop(tg, StubService(), stop=stop)
    assert tg.sleeps == [12]


async def test_other_api_errors_retry_with_backoff(tg):
    stop = asyncio.Event()
    tg.queue("getUpdates", api_error(502, "Bad Gateway"), stop_when_polled(stop))
    await run_loop(tg, StubService(), stop=stop)
    assert tg.sleeps == [1.0]


# --- startup checks ------------------------------------------------------------------------------------------------


async def test_prepare_returns_the_bot_username_when_no_webhook(tg):
    assert await prepare(tg.client(), delete_webhook=False) == "ruko_dev_bot"
    assert tg.method_calls("deleteWebhook") == []


async def test_prepare_refuses_when_a_webhook_is_registered(tg):
    tg.webhook_url = "https://prod.example.com/webhook/telegram"
    with pytest.raises(PollingError) as excinfo:
        await prepare(tg.client(), delete_webhook=False)
    assert "--delete-webhook" in str(excinfo.value)
    assert "separate development bot" in str(excinfo.value)
    assert tg.method_calls("deleteWebhook") == []  # the production webhook is untouched
    assert "prod.example.com" not in str(excinfo.value)


async def test_prepare_deletes_the_webhook_only_when_asked(tg, capsys):
    tg.webhook_url = "https://prod.example.com/webhook/telegram"
    await prepare(tg.client(), delete_webhook=True)
    assert tg.method_calls("deleteWebhook") == [{"drop_pending_updates": False}]
    assert tg.webhook_url == ""
    assert "Webhook deleted" in capsys.readouterr().out


async def test_prepare_reports_a_bad_token(tg):
    tg.queue("getMe", api_error(401, "Unauthorized"))
    with pytest.raises(PollingError, match="401"):
        await prepare(tg.client(), delete_webhook=False)


async def test_prepare_reports_network_trouble(tg):
    def boom(request):
        raise httpx.ConnectError("down", request=request)

    tg.queue("getMe", boom)
    with pytest.raises(PollingError, match="reach Telegram"):
        await prepare(tg.client(), delete_webhook=False)


# --- run() / main() ----------------------------------------------------------------------------------------------------


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **({"telegram_bot_token": TOKEN} | overrides))


async def test_run_without_a_token_exits_cleanly(capsys):
    assert await poll.run(settings(telegram_bot_token=None)) == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


async def test_run_reports_fatal_errors_without_leaking_the_token(tg, monkeypatch, capsys):
    monkeypatch.setattr(poll, "make_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(tg._handler)))
    tg.queue("getUpdates", api_error(409, "Conflict"))
    assert await poll.run(settings()) == 1
    captured = capsys.readouterr()
    assert "Polling as @ruko_dev_bot" in captured.out
    assert "409" in captured.err
    assert TOKEN not in captured.out + captured.err


async def test_run_refuses_to_start_over_a_registered_webhook(tg, monkeypatch, capsys):
    monkeypatch.setattr(poll, "make_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(tg._handler)))
    tg.webhook_url = "https://prod.example.com/webhook/telegram"
    assert await poll.run(settings()) == 1
    assert "--delete-webhook" in capsys.readouterr().err
    assert tg.method_calls("getUpdates") == []
    assert tg.method_calls("deleteWebhook") == []


def test_main_passes_flags_through(monkeypatch):
    seen = {}

    async def fake_run(settings, *, delete_webhook, poll_timeout):
        seen.update(delete_webhook=delete_webhook, poll_timeout=poll_timeout)
        return 0

    monkeypatch.setattr(poll, "run", fake_run)
    assert poll.main(["--delete-webhook", "--poll-timeout", "5"]) == 0
    assert seen == {"delete_webhook": True, "poll_timeout": 5}
    assert poll.main([]) == 0
    assert seen == {"delete_webhook": False, "poll_timeout": 30}


def test_main_exits_quietly_on_ctrl_c(monkeypatch, capsys):
    def interrupted(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(poll.asyncio, "run", interrupted)
    assert poll.main([]) == 0
    assert "Stopped" in capsys.readouterr().out
