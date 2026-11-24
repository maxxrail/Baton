"""conversation state

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-23 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("state", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("turn_lease_until", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_column("turn_lease_until")
        batch_op.drop_column("state")
