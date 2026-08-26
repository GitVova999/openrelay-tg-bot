"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-26
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EMBED_DIM = 384  # matches settings().embed_dim; kept in sync manually.


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "channels",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tg_chat_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("tier", sa.String(16), nullable=False, server_default="simple"),
        sa.Column("pay_mode", sa.String(16), nullable=False, server_default="treasury"),
        sa.Column("owner_user_id", sa.BigInteger),
        sa.Column("revenue_share_bps", sa.Integer, nullable=False, server_default="2000"),
        sa.Column("per_user_cap_usdmicro", sa.BigInteger, nullable=False, server_default="50000"),
        sa.Column("setup_paid_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_channels_tg_chat_id", "channels", ["tg_chat_id"], unique=True)

    op.create_table(
        "channel_wallets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("channel_id", sa.Integer, sa.ForeignKey("channels.id", ondelete="CASCADE"), unique=True),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column("address", sa.String(64), nullable=False),
        sa.Column("derivation_index", sa.Integer),
        sa.Column("balance_usdmicro", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("channel_id", sa.Integer, sa.ForeignKey("channels.id", ondelete="CASCADE")),
        sa.Column("tg_message_id", sa.BigInteger, nullable=False),
        sa.Column("sender_user_id", sa.BigInteger),
        sa.Column("sender_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("text", sa.Text, nullable=False, server_default=""),
        sa.Column("reply_to_tg_id", sa.BigInteger),
        sa.Column("forward_from", sa.String(128)),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("simhash", sa.BigInteger),
        sa.UniqueConstraint("channel_id", "tg_message_id", name="uq_channel_msg"),
    )
    op.create_index("ix_messages_channel_time", "messages", ["channel_id", "sent_at"])
    op.create_index("ix_messages_sent_at", "messages", ["sent_at"])
    op.create_index("ix_messages_simhash", "messages", ["simhash"])

    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("channel_id", sa.Integer, sa.ForeignKey("channels.id", ondelete="CASCADE")),
        sa.Column("origin_kind", sa.String(16), nullable=False),
        sa.Column("origin_ref", sa.dialects.postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("token_count", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_chunks_channel", "chunks", ["channel_id"])

    op.create_table(
        "embeddings",
        sa.Column("chunk_id", sa.Integer, sa.ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("model", sa.String(128), primary_key=True),
        sa.Column("vec", Vector(EMBED_DIM), nullable=False),
    )
    # IVFFlat / HNSW created after some data lands — creating on empty table is a no-op.
    # (See infra/postgres/init.sql for the CREATE INDEX ivfflat step run post-backfill.)

    op.create_table(
        "digests",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("channel_id", sa.Integer, sa.ForeignKey("channels.id", ondelete="CASCADE")),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("token_count", sa.Integer, nullable=False),
        sa.Column("covers_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("covers_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("channel_id", "scope", "scope_key", name="uq_digest"),
    )
    op.create_index("ix_digests_channel", "digests", ["channel_id"])

    op.create_table(
        "documents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("channel_id", sa.Integer, sa.ForeignKey("channels.id", ondelete="CASCADE")),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_ref", sa.String(2048), nullable=False),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("added_by_user_id", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_documents_channel", "documents", ["channel_id"])

    op.create_table(
        "tx_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("channel_id", sa.Integer, sa.ForeignKey("channels.id")),
        sa.Column("user_tg_id", sa.BigInteger),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("chain", sa.String(16), nullable=False),
        sa.Column("amount_usdmicro", sa.BigInteger, nullable=False),
        sa.Column("onchain_ref", sa.String(128)),
        sa.Column("request_id", sa.String(64)),
        sa.Column("meta", sa.dialects.postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tx_log_channel", "tx_log", ["channel_id"])
    op.create_index("ix_tx_log_user", "tx_log", ["user_tg_id"])
    op.create_index("ix_tx_log_request", "tx_log", ["request_id"])
    op.create_index("ix_tx_log_created", "tx_log", ["created_at"])

    op.create_table(
        "usage",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("channel_id", sa.Integer, sa.ForeignKey("channels.id", ondelete="CASCADE")),
        sa.Column("user_tg_id", sa.BigInteger),
        sa.Column("command", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usdmicro", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("payer", sa.String(16), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_usage_channel", "usage", ["channel_id"])
    op.create_index("ix_usage_user", "usage", ["user_tg_id"])
    op.create_index("ix_usage_request", "usage", ["request_id"])
    op.create_index("ix_usage_created", "usage", ["created_at"])


def downgrade() -> None:
    for t in ("usage", "tx_log", "documents", "digests", "embeddings", "chunks",
              "messages", "channel_wallets", "channels"):
        op.drop_table(t)
    op.execute("DROP EXTENSION IF EXISTS vector")
