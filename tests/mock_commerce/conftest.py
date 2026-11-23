from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mock_commerce.app import create_app
from mock_commerce.db import make_session_factory

FIXED_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A freshly seeded store on a temp SQLite file with a frozen clock."""
    factory = make_session_factory(f"sqlite:///{(tmp_path / 'mc.db').as_posix()}")
    app = create_app(session_factory=factory, clock=lambda: FIXED_NOW)
    with TestClient(app) as c:
        yield c
