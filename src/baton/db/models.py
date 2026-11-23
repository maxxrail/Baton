"""SQLAlchemy models for Baton's own data: conversations, messages and traces.

Only portable column types are used so the same schema works on SQLite (dev/tests) and
Postgres (production). Enums are stored as plain strings with a CHECK constraint.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, Dialect, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class UTCDateTime(TypeDecorator[datetime]):
    """A timezone-aware UTC datetime on every database.

    Postgres returns aware datetimes but SQLite returns naive ones; this makes both aware, so
    comparisons never break on only one of them. Naive inputs are rejected.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use an aware UTC datetime")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class ConversationStatus(enum.StrEnum):
    OPEN = "open"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


class MessageRole(enum.StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    HUMAN_AGENT = "human_agent"
    SYSTEM = "system"


def _in_check(column: str, values: type[enum.StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(_in_check("status", ConversationStatus), name="ck_conversations_status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    channel: Mapped[str] = mapped_column(String(32))
    customer_ref: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default=ConversationStatus.OPEN.value)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", order_by="Message.id", cascade="all, delete-orphan"
    )
    traces: Mapped[list["Trace"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    """A message in a conversation.

    The integer id is the message order. Timestamps can't be used for that because several
    messages in one turn can share a clock tick (Windows ticks every ~15 ms).
    """

    __tablename__ = "messages"
    __table_args__ = (CheckConstraint(_in_check("role", MessageRole), name="ck_messages_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Trace(Base):
    """One agent turn's debug record. `payload` shape is defined by the runtime (Part 02)."""

    __tablename__ = "traces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="traces")
