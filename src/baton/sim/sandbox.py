"""A sandbox: one worker's private mock store and Baton database.

The simulator gives every worker thread its own sandbox and **resets it to the seed before
every run**, so runs are independent: a refund issued in run 1 is gone in run 2, and two runs
in parallel never share a store. The store runs in-process (Starlette's `TestClient` calls the
ASGI app directly, no sockets), with a clock frozen when the suite started, so every reset
produces byte-identical data. `reset()` returns a fingerprint of the store's tables, recorded
on every result, which is how a report proves two runs started from the same state.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import cast

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from baton.db import Base, make_engine, make_session_factory
from baton.tools.client import CommerceClient
from mock_commerce.app import create_app as create_mock_commerce
from mock_commerce.db import make_session_factory as make_store_sessions
from mock_commerce.models import Base as StoreBase
from mock_commerce.seed import reset_and_seed


class Sandbox:
    def __init__(self, root: Path, now: datetime) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.now = now
        self.store_sessions = make_store_sessions(f"sqlite:///{(root / 'store.db').as_posix()}")
        app = create_mock_commerce(
            session_factory=self.store_sessions, clock=lambda: now, seed_on_startup=False
        )
        self._http = TestClient(app)
        self._http.__enter__()  # runs the app's startup (creates the tables)
        self.engine: Engine = make_engine(f"sqlite:///{(root / 'baton.db').as_posix()}")
        self.sessions: sessionmaker[Session] = make_session_factory(self.engine)

    def reset(self) -> str:
        """Reseed the store, empty the Baton database, and fingerprint the store."""
        reset_and_seed(self.store_sessions, now=self.now)
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        return self.fingerprint()

    def fingerprint(self) -> str:
        """sha256 over every store row, table by table, in primary-key order."""
        digest = hashlib.sha256()
        with self.store_sessions() as session:
            for table in StoreBase.metadata.sorted_tables:
                order = list(table.primary_key.columns) or list(table.columns)
                for row in session.execute(select(table).order_by(*order)).mappings():
                    line = json.dumps({"t": table.name, **row}, sort_keys=True, default=str)
                    digest.update(line.encode("utf-8"))
        return digest.hexdigest()[:16]

    def commerce(self) -> CommerceClient:
        # Starlette's TestClient behaves as an httpx.Client (same request API), in-process.
        return CommerceClient(cast(httpx.Client, self._http))

    def store_get(self, path: str) -> dict[str, object]:
        """Read the store directly (tests and debugging)."""
        data: dict[str, object] = self._http.get(path).json()
        return data

    def close(self) -> None:
        self._http.__exit__(None, None, None)
        self.engine.dispose()
        bind = self.store_sessions.kw.get("bind")
        if isinstance(bind, Engine):
            bind.dispose()
