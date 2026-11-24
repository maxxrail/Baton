"""Suite-wide test setup.

Tests must run offline: any socket connection to a non-loopback address fails loudly, and
the LLM is forced to the fake so a developer's real key in `.env` is never used.
"""

import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baton.config import get_settings
from baton.tools.client import CommerceClient
from mock_commerce.app import create_app as create_mock_commerce
from mock_commerce.db import make_session_factory as make_mc_session_factory

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


# ---------------------------------------------------------------- shared fixtures

STORE_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def store_http(tmp_path: Path) -> Iterator[TestClient]:
    """The Northwind mock store, seeded, running in-process (no sockets).

    Starlette's `TestClient` is an `httpx.Client` whose transport calls the ASGI app directly,
    so Baton's `CommerceClient` uses it exactly like a real HTTP client.
    """
    factory = make_mc_session_factory(f"sqlite:///{(tmp_path / 'store.db').as_posix()}")
    app = create_mock_commerce(session_factory=factory, clock=lambda: STORE_NOW)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def commerce(store_http: TestClient) -> CommerceClient:
    return CommerceClient(store_http)
