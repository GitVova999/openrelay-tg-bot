"""channel prefs: language, tone, topics, system_prompt override

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("channels", sa.Column("language", sa.String(8), nullable=False, server_default="ru"))
    op.add_column("channels", sa.Column("tone", sa.String(256), nullable=False, server_default=""))
    op.add_column("channels", sa.Column("topics", sa.String(512), nullable=False, server_default=""))
    op.add_column("channels", sa.Column("system_prompt", sa.Text, nullable=True))


def downgrade() -> None:
    for col in ("system_prompt", "topics", "tone", "language"):
        op.drop_column("channels", col)
