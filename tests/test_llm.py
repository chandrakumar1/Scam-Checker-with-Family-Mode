import asyncio
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app.analyzer import llm
from app.analyzer.llm import LLMAnalysis, _anthropic_client as real_anthropic_client, analyze
from app.analyzer.prompts import SYSTEM_PROMPT
from app.config import Settings

pytestmark = pytest.mark.anyio

FAKE_KEY = "sk-ant-test-DO-NOT-LOG-123"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

VALID = {
    "verdict": "likely_scam",
    "scam_type": "otp_fraud",
    "red_flags": ["Asks for OTP"],
    "explanation": "This message asks for your OTP. Banks never do that.",
    "what_to_do": ["Do not share the OTP.", "Call 1930."],
}


def make_settings(**overrides) -> Settings:
    values = {"llm_provider": "anthropic", "llm_api_key": FAKE_KEY} | overrides
    return Settings(_env_file=None, **values)


def text_response(*texts: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=t) for t in texts],
    )


class FakeClient:
    """Stands in for anthropic.AsyncAnthropic; records calls, returns or raises."""

    def __init__(self, response=None, error: Exception | None = None):
        self.calls: list[dict] = []
        self.closed = False
        self._response = response
        self._error = error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._response

    async def close(self):
        self.closed = True


@pytest.fixture
def use_client(monkeypatch):
    def install(client):
        monkeypatch.setattr(llm, "_anthropic_client", lambda settings: client)
        return client

    return install


REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


# --- success ------------------------------------------------------------------


async def test_success_returns_validated_model(use_client):
    client = use_client(FakeClient(text_response(json.dumps(VALID))))
    result = await analyze("Share your OTP", settings=make_settings())
    assert isinstance(result, LLMAnalysis)
    assert result.verdict == "likely_scam"
    assert result.what_to_do == VALID["what_to_do"]
    assert len(client.calls) == 1
    assert client.closed


async def test_request_shape(use_client):
    client = use_client(FakeClient(text_response(json.dumps(VALID))))
    await analyze("Share your OTP", language="hi", settings=make_settings(llm_model="m-1", llm_effort="medium"))

    call = client.calls[0]
    assert call["model"] == "m-1"
    assert call["system"] == SYSTEM_PROMPT
    assert call["output_config"]["effort"] == "medium"
    schema = call["output_config"]["format"]["schema"]
    assert schema["properties"]["verdict"]["enum"] == ["likely_scam", "suspicious", "no_obvious_red_flags"]
    assert schema["additionalProperties"] is False
    # Server-side refusal fallback is preserved.
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]

    [message] = call["messages"]
    assert message["role"] == "user"
    [text_block] = message["content"]
    assert "Reply language: Hindi" in text_block["text"]
    assert "<untrusted_message>\nShare your OTP\n</untrusted_message>" in text_block["text"]


async def test_uses_last_text_block_after_fallback(use_client):
    other = VALID | {"verdict": "suspicious"}
    use_client(FakeClient(text_response('{"partial', json.dumps(other))))
    assert (await analyze("hi", settings=make_settings())).verdict == "suspicious"


async def test_image_is_sent_before_text(use_client):
    client = use_client(FakeClient(text_response(json.dumps(VALID))))
    assert await analyze(image_bytes=PNG_BYTES, settings=make_settings()) is not None
    image_block, text_block = client.calls[0]["messages"][0]["content"]
    assert image_block["type"] == "image"
    assert image_block["source"]["media_type"] == "image/png"
    assert "screenshot" in text_block["text"]
    assert "<untrusted_message>" not in text_block["text"]


async def test_real_sdk_serialises_request_without_network(monkeypatch):
    """Drive the real AsyncAnthropic SDK against a mock transport."""
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta")
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": json.dumps(VALID)}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = anthropic.AsyncAnthropic(
        api_key=FAKE_KEY,
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    monkeypatch.setattr(llm, "_anthropic_client", lambda settings: client)

    result = await analyze("Share your OTP", settings=make_settings())
    assert result is not None and result.verdict == "likely_scam"
    assert seen["beta"] == "server-side-fallback-2026-07-01"
    assert seen["body"]["fallbacks"] == "default"
    assert seen["body"]["output_config"]["format"]["type"] == "json_schema"
    assert seen["body"]["system"] == SYSTEM_PROMPT


# --- failures -> None -----------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APITimeoutError(request=REQUEST),
        anthropic.APIConnectionError(request=REQUEST),
        anthropic.InternalServerError("boom", response=httpx2.Response(500, request=REQUEST), body=None),
        anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=REQUEST), body=None),
        anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=REQUEST), body=None),
        RuntimeError("unexpected provider bug"),
    ],
    ids=["timeout", "connection", "500", "429", "401", "unexpected"],
)
async def test_api_failures_return_none(use_client, error):
    client = use_client(FakeClient(error=error))
    assert await analyze("Share your OTP", settings=make_settings()) is None
    assert client.closed


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '{"verdict": "likely_scam"',  # truncated
        json.dumps(VALID | {"verdict": "safe"}),  # "safe" is never allowed
        json.dumps(VALID | {"verdict": "scam"}),
        json.dumps({k: v for k, v in VALID.items() if k != "explanation"}),
        json.dumps(VALID | {"extra_field": "x"}),
        json.dumps(VALID | {"red_flags": "not a list"}),
        "",
    ],
    ids=["garbage", "truncated", "safe", "unknown-verdict", "missing-field", "extra-field", "wrong-type", "empty"],
)
async def test_invalid_json_returns_none(use_client, raw):
    use_client(FakeClient(text_response(raw)))
    assert await analyze("hello", settings=make_settings()) is None


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
async def test_unusable_stop_reason_returns_none(use_client, stop_reason):
    use_client(FakeClient(text_response(json.dumps(VALID), stop_reason=stop_reason)))
    assert await analyze("hello", settings=make_settings()) is None


async def test_no_text_block_returns_none(use_client):
    use_client(FakeClient(SimpleNamespace(stop_reason="end_turn", content=[])))
    assert await analyze("hello", settings=make_settings()) is None


async def test_blocked_real_client_returns_none():
    # conftest blocks the real client; analyze must still degrade to None.
    assert await analyze("hello", settings=make_settings()) is None


# --- no call made -----------------------------------------------------------------


@pytest.mark.parametrize("text", [None, "", "   "])
async def test_nothing_to_analyze(use_client, text):
    client = use_client(FakeClient(text_response(json.dumps(VALID))))
    assert await analyze(text, settings=make_settings()) is None
    assert client.calls == []


async def test_provider_none_disables_llm(use_client):
    client = use_client(FakeClient(text_response(json.dumps(VALID))))
    assert await analyze("hello", settings=make_settings(llm_provider="none")) is None
    assert client.calls == []


async def test_unsupported_image_skips_llm(use_client):
    client = use_client(FakeClient(text_response(json.dumps(VALID))))
    assert await analyze(image_bytes=b"%PDF-1.4 not an image", settings=make_settings()) is None
    assert client.calls == []


@pytest.mark.parametrize(
    ("data", "media_type"),
    [
        (PNG_BYTES, "image/png"),
        (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        (b"GIF89a....", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"hello", None),
    ],
)
def test_detect_image_type(data, media_type):
    assert llm.detect_image_type(data) == media_type


# --- config & secrets -------------------------------------------------------------


def test_default_timeout_is_15_seconds():
    assert Settings(_env_file=None).llm_timeout_seconds == 15


def test_client_has_5s_connect_timeout_inside_overall_deadline_and_no_retries():
    client = real_anthropic_client(make_settings())
    assert isinstance(client, anthropic.AsyncAnthropic)
    assert client.timeout.connect == 5.0
    assert client.timeout.read == 15.0
    assert client.max_retries == 0


def test_connect_timeout_never_exceeds_overall_deadline():
    client = real_anthropic_client(make_settings(llm_timeout_seconds=2.0))
    assert client.timeout.connect == 2.0


def test_api_key_is_secret():
    settings = make_settings()
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings.model_dump())


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APITimeoutError(request=REQUEST),
        anthropic.AuthenticationError(f"bad key {FAKE_KEY}", response=httpx2.Response(401, request=REQUEST), body=None),
        RuntimeError(f"leaky error {FAKE_KEY}"),
    ],
    ids=["timeout", "401", "unexpected"],
)
async def test_api_key_never_logged(use_client, caplog, error):
    use_client(FakeClient(error=error))
    with caplog.at_level(logging.DEBUG):
        await analyze("hello", settings=make_settings())
    assert caplog.records, "expected a warning to be logged"
    assert FAKE_KEY not in caplog.text


# --- one shared wall-clock deadline ------------------------------------------------

DEADLINE = 1.0


class SlowServer:
    """Local 127.0.0.1 server that answers /v1/messages slowly.

    mode "silent": accept the request, send nothing for `silent_for` seconds.
    mode "drip":   send the response one chunk every `interval` seconds, so
                   every individual read stays well under a per-read timeout.
    """

    def __init__(self, mode: str, *, silent_for: float = 10.0, interval: float = 0.3, chunks: int = 10):
        self.mode = mode
        self.received = threading.Event()
        self.stop = threading.Event()
        self.total_drip_seconds = interval * chunks
        outer = self
        body = json.dumps(
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": json.dumps(VALID)}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ).encode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                outer.received.set()
                try:
                    if outer.mode == "silent":
                        outer.stop.wait(silent_for)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.flush()
                    step = len(body) // chunks + 1
                    for i in range(0, len(body), step):
                        if outer.stop.wait(interval):
                            return
                        self.wfile.write(body[i : i + step])
                        self.wfile.flush()
                except OSError:
                    pass  # client went away after the deadline

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def slow_server():
    servers = []

    def start(mode, **kwargs):
        server = SlowServer(mode, **kwargs)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


@pytest.fixture
def point_client_at(monkeypatch):
    """Use the REAL client factory (real timeouts, retries) against a local URL."""
    clients = []

    def install(server):
        def factory(settings):
            client = real_anthropic_client(settings).copy(base_url=server.url)
            clients.append(client)
            return client

        monkeypatch.setattr(llm, "_anthropic_client", factory)
        return clients

    return install


async def test_silent_server_is_cut_off_at_overall_deadline(slow_server, point_client_at, caplog):
    server = slow_server("silent")
    point_client_at(server)
    settings = make_settings(llm_timeout_seconds=DEADLINE)

    start = time.monotonic()
    with caplog.at_level(logging.WARNING):
        result = await analyze("Share your OTP", settings=settings)
    elapsed = time.monotonic() - start

    assert server.received.is_set()  # the request really went out
    assert result is None
    assert DEADLINE * 0.9 <= elapsed < DEADLINE + 1.0
    assert FAKE_KEY not in caplog.text


async def test_trickling_response_cannot_outlive_the_deadline(slow_server, point_client_at, caplog):
    """The case a per-read timeout misses.

    The server sends a byte chunk every 0.3s, so no single read ever reaches
    the 1s read timeout and, without the overall deadline, the call would
    complete after ~3s. Only a shared wall-clock budget stops it at 1s.
    """
    server = slow_server("drip", interval=0.3, chunks=10)
    clients = point_client_at(server)
    settings = make_settings(llm_timeout_seconds=DEADLINE)
    assert server.total_drip_seconds > 2.5 * DEADLINE

    start = time.monotonic()
    with caplog.at_level(logging.WARNING):
        result = await analyze("Share your OTP", settings=settings)
    elapsed = time.monotonic() - start

    assert server.received.is_set()
    assert result is None
    assert DEADLINE * 0.9 <= elapsed < DEADLINE + 1.0, elapsed
    assert "overall deadline" in caplog.text
    # The cancelled call's HTTP client was closed, not leaked.
    assert clients and clients[0].is_closed()


async def test_same_trickling_response_succeeds_given_enough_time(slow_server, point_client_at):
    """Control for the test above: the server and parsing are fine, only the
    deadline made the previous call fail."""
    server = slow_server("drip", interval=0.1, chunks=5)
    point_client_at(server)
    result = await analyze("Share your OTP", settings=make_settings(llm_timeout_seconds=10.0))
    assert result is not None and result.verdict == "likely_scam"


async def test_deadline_cancels_any_slow_provider(monkeypatch):
    """The deadline lives in analyze(), so it bounds every provider."""
    cancelled = asyncio.Event()

    async def hanging_provider(system, user_prompt, image, settings):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setitem(llm.PROVIDERS, "anthropic", hanging_provider)
    start = time.monotonic()
    result = await analyze("hello", settings=make_settings(llm_timeout_seconds=0.3))
    assert result is None
    assert time.monotonic() - start < 1.5
    assert cancelled.is_set()


async def test_fast_call_is_not_affected_by_deadline(use_client):
    use_client(FakeClient(text_response(json.dumps(VALID))))
    assert await analyze("hi", settings=make_settings(llm_timeout_seconds=0.5)) is not None
