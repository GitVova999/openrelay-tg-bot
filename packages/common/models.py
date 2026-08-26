"""ORM models — one table per concept. Kept in one file while it stays small.

Design notes:
- `channels.tier` = "simple" (custodial) | "docker" (non-custodial).
- `channels.pay_mode` = "treasury" | "user" | "hybrid".
- Money everywhere is stored as integer µUSD (10^-6 USD), never float.
- `messages` carries only what we need for retrieval + audit; media stays in TG.
- `chunks` is the retrieval unit — one row per embedded slice. `origin_ref`
  links back to the source (a message range, a doc page).
- `embeddings` is separate so we can rotate models without rewriting `chunks`.
- `digests` are our hierarchical summaries — per-day, per-week, per-topic.
"""

from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import settings


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB}


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- channels

class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    tier: Mapped[str] = mapped_column(String(16), default="simple")  # simple|docker
    pay_mode: Mapped[str] = mapped_column(String(16), default="treasury")  # treasury|user|hybrid
    owner_user_id: Mapped[int | None] = mapped_column(BigInteger)  # TG user id of owner
    revenue_share_bps: Mapped[int] = mapped_column(Integer, default=2000)  # 20% default
    per_user_cap_usdmicro: Mapped[int] = mapped_column(BigInteger, default=50_000)  # $0.05
    setup_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    messages: Mapped[list["Message"]] = relationship(back_populates="channel")
    wallet: Mapped["ChannelWallet | None"] = relationship(back_populates="channel", uselist=False)


class ChannelWallet(Base):
    """Custodial subaccount for Simple-tier, or watched external addr for Docker."""

    __tablename__ = "channel_wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), unique=True)
    chain: Mapped[str] = mapped_column(String(16))  # base|solana|stars
    address: Mapped[str] = mapped_column(String(64))
    # Encrypted derivation salt for custodial; NULL for docker (non-custodial).
    derivation_index: Mapped[int | None] = mapped_column(Integer)
    balance_usdmicro: Mapped[int] = mapped_column(BigInteger, default=0)  # cached; source is on-chain
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    channel: Mapped[Channel] = relationship(back_populates="wallet")


# --------------------------------------------------------------------------- ingest

class Message(Base):
    """One TG message. Media not stored; we only need text for retrieval."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("channel_id", "tg_message_id", name="uq_channel_msg"),
        Index("ix_messages_channel_time", "channel_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    tg_message_id: Mapped[int] = mapped_column(BigInteger)
    sender_user_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_name: Mapped[str] = mapped_column(String(128), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    reply_to_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    forward_from: Mapped[str | None] = mapped_column(String(128))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Dedup marker — SimHash of the normalized text (int64).
    simhash: Mapped[int | None] = mapped_column(BigInteger, index=True)

    channel: Mapped[Channel] = relationship(back_populates="messages")


class Chunk(Base):
    """Retrieval unit — a slice of one or more messages, or a doc page.

    `origin_kind = "msg_range"` → `origin_ref = {message_ids: [..], span: "..."}`.
    `origin_kind = "doc"`       → `origin_ref = {doc_id: N, page: K, span: "..."}`.
    `origin_kind = "digest"`    → `origin_ref = {digest_id: N}` (hierarchical).
    """

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    origin_kind: Mapped[str] = mapped_column(String(16))  # msg_range|doc|digest
    origin_ref: Mapped[dict[str, Any]] = mapped_column(default=dict)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    embedding: Mapped["Embedding | None"] = relationship(back_populates="chunk", uselist=False)


class Embedding(Base):
    """Kept separate so we can rotate models without rewriting chunks."""

    __tablename__ = "embeddings"

    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    vec: Mapped[list[float]] = mapped_column(Vector(settings().embed_dim))

    chunk: Mapped[Chunk] = relationship(back_populates="embedding")


class Digest(Base):
    """Hierarchical summary. `scope = "day"|"week"|"topic"`."""

    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(16))
    scope_key: Mapped[str] = mapped_column(String(64))  # "2026-08-26" | "2026-W34" | slug
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    covers_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    covers_to: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("channel_id", "scope", "scope_key", name="uq_digest"),)


class Document(Base):
    """Uploaded doc — pdf, docx, html url."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    source_kind: Mapped[str] = mapped_column(String(16))  # pdf|docx|url|text
    source_ref: Mapped[str] = mapped_column(String(2048))  # URL or filename
    title: Mapped[str] = mapped_column(String(256), default="")
    added_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# --------------------------------------------------------------------------- billing

class TxLog(Base):
    """Every USDC/Stars in-or-out transaction. Source of truth for accounting."""

    __tablename__ = "tx_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), index=True)
    user_tg_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    direction: Mapped[str] = mapped_column(String(8))  # in|out
    kind: Mapped[str] = mapped_column(String(32))  # deposit|inference|setup_fee|refund|split_owner
    chain: Mapped[str] = mapped_column(String(16))  # base|solana|stars
    amount_usdmicro: Mapped[int] = mapped_column(BigInteger)
    onchain_ref: Mapped[str | None] = mapped_column(String(128))  # tx hash / signature
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)  # correlates inference calls
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class UsageRecord(Base):
    """One row per served request. Used for rate limits + billing correlation."""

    __tablename__ = "usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    user_tg_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    command: Mapped[str] = mapped_column(String(32))  # summarize|ask|faq
    model: Mapped[str] = mapped_column(String(128))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usdmicro: Mapped[int] = mapped_column(BigInteger, default=0)
    payer: Mapped[str] = mapped_column(String(16))  # treasury|user|stars
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
