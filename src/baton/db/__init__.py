"""Database layer: models and sessions. Schema changes go through Alembic (`migrations/`)."""

from baton.db.models import (
    AOPVersion,
    Base,
    ChannelThread,
    Conversation,
    ConversationStatus,
    KBChunk,
    Message,
    MessageRole,
    SmsOptOut,
    Trace,
)
from baton.db.session import (
    default_session_factory,
    get_session,
    make_engine,
    make_session_factory,
    session_scope,
)

__all__ = [
    "AOPVersion",
    "Base",
    "ChannelThread",
    "Conversation",
    "ConversationStatus",
    "KBChunk",
    "Message",
    "MessageRole",
    "SmsOptOut",
    "Trace",
    "default_session_factory",
    "get_session",
    "make_engine",
    "make_session_factory",
    "session_scope",
]
