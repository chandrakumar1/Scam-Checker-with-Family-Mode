import socket

import pytest

from app.analyzer import llm

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture
def anyio_backend():
    # Async tests (marked with pytest.mark.anyio) run on asyncio only.
    return "asyncio"


@pytest.fixture(autouse=True)
def _block_real_llm_client(monkeypatch):
    """No test may reach a real LLM API. Tests that exercise the provider
    install their own fake client on top of this."""

    def _blocked(settings):
        raise RuntimeError("real LLM client is blocked during tests")

    monkeypatch.setattr(llm, "_anthropic_client", _blocked)


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """configure_logging() changes global logger state (httpx levels, and a
    redaction filter on the log handlers, which pytest keeps for the whole
    session). Undo it after every test so no test depends on the order the
    others ran in."""
    import logging

    from app.logging_config import FILTERED_LOGGERS, NOISY_LOGGERS, RedactSecretsFilter

    names = ["app", *NOISY_LOGGERS]
    levels = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    for name in FILTERED_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            for existing in [f for f in handler.filters if isinstance(f, RedactSecretsFilter)]:
                handler.removeFilter(existing)


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch):
    """No test may open a connection to anything but this machine, so a
    mistake can never reach the real Telegram or LLM APIs. Loopback is allowed
    for the local slow-server tests."""
    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0] if isinstance(address, tuple) else str(address)
            if host not in _LOOPBACK_HOSTS and not host.startswith("127."):
                raise RuntimeError(f"external network access is blocked during tests (host {host!r})")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
