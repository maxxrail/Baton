"""Suite-wide test setup.

Tests must run offline: any socket connection to a non-loopback address fails loudly, and
the LLM is forced to the fake so a developer's real key in `.env` is never used.
"""

import socket

import pytest

from baton.config import get_settings

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guard(address: object) -> None:
    host = address[0] if isinstance(address, tuple) else address
    if host not in _LOOPBACK:
        raise RuntimeError(f"tests must not use the network (tried to connect to {address!r})")


def _connect(self: socket.socket, address: object) -> None:
    _guard(address)
    _real_connect(self, address)  # type: ignore[arg-type]


def _connect_ex(self: socket.socket, address: object) -> int:
    _guard(address)
    return _real_connect_ex(self, address)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _connect_ex)
    monkeypatch.setenv("BATON_LLM", "fake")
    # An empty env var beats a real key in .env (env vars take precedence over the dotenv file).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
