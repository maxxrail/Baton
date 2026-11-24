"""channels: contact + subject on conversations, message meta, channel threads, SMS opt-outs

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-24 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("contact", sa.String(length=254), nullable=True))
        batch_op.add_column(sa.Column("subject", sa.String(length=300), nullable=True))
        batch_op.create_index(batch_op.f("ix_conversations_contact"), ["contact"], unique=False)
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("meta", sa.JSON(), nullable=True))

    op.create_table(
        "channel_threads",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("thread_key", sa.String(length=300), nullable=False),
        sa.Column("conversation_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel", "thread_key", name="uq_channel_threads_key"),
    )
    with op.batch_alter_table("channel_threads", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_channel_threads_conversation_id"), ["conversation_id"], unique=False
        )

    op.create_table(
        "sms_opt_outs",
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("phone"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("sms_opt_outs")
    with op.batch_alter_table("channel_threads", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_channel_threads_conversation_id"))
    op.drop_table("channel_threads")
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_column("meta")
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_conversations_contact"))
        batch_op.drop_column("subject")
        batch_op.drop_column("contact")
