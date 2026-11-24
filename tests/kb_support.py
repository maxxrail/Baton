"""Shared help-center test helpers (fixtures that use them live in tests/conftest.py)."""

from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from baton.db import Base, make_engine, make_session_factory

REPO = Path(__file__).resolve().parents[1]
KB_DIR = REPO / "data" / "kb"


def make_kb_sessions(path: Path) -> sessionmaker[Session]:
    engine = make_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)
