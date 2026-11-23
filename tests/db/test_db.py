from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session, sessionmaker

from baton.db import (
    Base,
    Conversation,
    ConversationStatus,
    Message,
    MessageRole,
    Trace,
    make_engine,
    make_session_factory,
    session_scope,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Engine:
    url = f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}"
    command.upgrade(_alembic_config(url), "head")
    return make_engine(url)


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(migrated_engine)


def test_upgrade_head_on_fresh_sqlite_creates_tables(migrated_engine: Engine) -> None:
    tables = set(inspect(migrated_engine).get_table_names())
    assert {"conversations", "messages", "traces", "alembic_version"} <= tables


def test_migrations_match_models(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models and migrations disagree; run alembic autogenerate: {diff}"


def test_downgrade_to_base(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'down.db').as_posix()}"
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    assert set(inspect(make_engine(url)).get_table_names()) == {"alembic_version"}


def test_conversation_message_trace_round_trip(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as s:
        convo = Conversation(channel="web", customer_ref="cus_0001")
        convo.messages.append(Message(role=MessageRole.CUSTOMER, content="Where is my order?"))
        s.add(convo)
        s.flush()
        s.add(
            Trace(
                conversation_id=convo.id,
                message_id=convo.messages[0].id,
                payload={"aop": "order_status", "tool_calls": [{"name": "get_order"}]},
            )
        )
        convo_id = convo.id

    with session_scope(factory) as s:
        loaded = s.get(Conversation, convo_id)
        assert loaded is not None
        assert loaded.status == ConversationStatus.OPEN
        assert loaded.created_at is not None
        assert [m.content for m in loaded.messages] == ["Where is my order?"]
        assert loaded.traces[0].payload["aop"] == "order_status"


def test_status_check_constraint(factory: sessionmaker[Session]) -> None:
    with pytest.raises(IntegrityError), session_scope(factory) as s:
        s.add(Conversation(channel="web", status="bogus"))


def test_role_check_constraint(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as s:
        convo = Conversation(channel="sms")
        s.add(convo)
    with pytest.raises(IntegrityError), session_scope(factory) as s:
        s.add(Message(conversation_id=convo.id, role="robot", content="x"))


def test_foreign_keys_enforced_on_sqlite(factory: sessionmaker[Session]) -> None:
    with pytest.raises(IntegrityError), session_scope(factory) as s:
        s.add(Message(conversation_id="does-not-exist", role=MessageRole.AGENT, content="x"))


def test_session_scope_rolls_back_on_error(factory: sessionmaker[Session]) -> None:
    def add_then_fail() -> None:
        with session_scope(factory) as s:
            s.add(Conversation(channel="email"))
            s.flush()
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        add_then_fail()
    with session_scope(factory) as s:
        assert s.scalars(select(Conversation)).all() == []


def test_messages_keep_insertion_order_within_one_clock_tick(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as s:
        convo = Conversation(channel="web")
        for i in range(20):  # far faster than a Windows clock tick
            convo.messages.append(Message(role=MessageRole.CUSTOMER, content=f"m{i}"))
        s.add(convo)
    with session_scope(factory) as s:
        loaded = s.get(Conversation, convo.id)
        assert loaded is not None
        assert [m.content for m in loaded.messages] == [f"m{i}" for i in range(20)]


def test_datetimes_come_back_timezone_aware(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as s:
        convo = Conversation(channel="web")
        s.add(convo)
    with session_scope(factory) as s:
        loaded = s.get(Conversation, convo.id)
        assert loaded is not None
        assert loaded.created_at.tzinfo is not None
        assert loaded.created_at.utcoffset() == timedelta(0)


def test_naive_datetimes_are_rejected(factory: sessionmaker[Session]) -> None:
    with pytest.raises(StatementError, match="naive datetime"), session_scope(factory) as s:
        s.add(Conversation(channel="web", created_at=datetime(2026, 1, 1)))
