"""Telethon-based channel ingest.

Backfill + real-time. Uses a user session (MTProto), not a bot token, because
bot tokens can only see messages after they're added and cannot pull history.
The session file (~openrelay_ingest.session) stays on disk after first login.

Run:
    python -m packages.context_svc.ingest backfill <tg_chat_id>
    python -m packages.context_svc.ingest watch                # all registered channels
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime

from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.tl.types import Message as TgMessage

from packages.common.config import settings
from packages.common.db import session
from packages.common.models import Channel, Message

log = logging.getLogger("ingest")


def _simhash64(text: str) -> int:
    """Cheap 64-bit SimHash — good enough to catch dup forwards/copy-pastes.

    Real SimHash uses shingles; we use whitespace tokens here for MVP speed.
    """
    if not text:
        return 0
    v = [0] * 64
    for tok in text.lower().split():
        h = hash(tok) & ((1 << 64) - 1)
        for i in range(64):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i, x in enumerate(v):
        if x > 0:
            out |= 1 << i
    # Postgres BIGINT is signed — remap to signed range.
    return out - (1 << 64) if out >= (1 << 63) else out


def _extract_text(msg: TgMessage) -> str:
    """Prefer .text over .message; fall back to caption for media."""
    return (msg.text or msg.message or "") or ""


async def _client() -> TelegramClient:
    s = settings()
    c = TelegramClient(s.tg_session_name, s.tg_api_id, s.tg_api_hash)
    await c.start()
    return c


async def _ensure_channel(chat_id: int, title: str = "") -> int:
    """Return internal channels.id for the given tg_chat_id, inserting if new."""
    async with session() as s:
        row = (await s.execute(select(Channel).where(Channel.tg_chat_id == chat_id))).scalar_one_or_none()
        if row:
            if title and not row.title:
                row.title = title
            return row.id
        row = Channel(tg_chat_id=chat_id, title=title)
        s.add(row)
        await s.flush()
        return row.id


async def _persist(channel_id: int, msg: TgMessage) -> bool:
    text = _extract_text(msg).strip()
    if not text:
        return False  # media-only: skip for retrieval (still counted in TG)
    sent_at = msg.date if isinstance(msg.date, datetime) else datetime.utcnow()
    sender_name = ""
    if msg.sender is not None:
        sender_name = getattr(msg.sender, "username", None) or (
            f"{getattr(msg.sender, 'first_name', '') or ''} {getattr(msg.sender, 'last_name', '') or ''}"
        ).strip()

    row = Message(
        channel_id=channel_id,
        tg_message_id=msg.id,
        sender_user_id=getattr(msg.sender_id, "user_id", None) if hasattr(msg.sender_id, "user_id") else msg.sender_id,
        sender_name=sender_name[:128],
        text=text,
        reply_to_tg_id=msg.reply_to_msg_id if msg.reply_to else None,
        forward_from=(getattr(msg.forward, "from_name", None) if msg.forward else None),
        sent_at=sent_at,
        simhash=_simhash64(text),
    )
    async with session() as s:
        # ON CONFLICT DO NOTHING via merge-like check: rely on uq_channel_msg.
        s.add(row)
        try:
            await s.flush()
            return True
        except Exception as e:
            # Duplicate — ignore.
            log.debug("dup or fail: %s", e)
            await s.rollback()
            return False


async def backfill(chat_id: int, limit: int | None = None) -> int:
    """Pull full history (or `limit` most recent) into DB."""
    c = await _client()
    try:
        entity = await c.get_entity(chat_id)
        title = getattr(entity, "title", "") or getattr(entity, "username", "") or ""
        channel_id = await _ensure_channel(chat_id, title)
        log.info("backfilling chat=%s (channels.id=%s) title=%r", chat_id, channel_id, title)

        count = 0
        async for msg in c.iter_messages(entity, limit=limit, reverse=True):
            if await _persist(channel_id, msg):
                count += 1
            if count and count % 100 == 0:
                log.info("  … %d messages", count)
        log.info("done: %d messages ingested from chat=%s", count, chat_id)
        return count
    finally:
        await c.disconnect()


async def watch() -> None:
    """Long-lived listener for new messages across all registered channels."""
    c = await _client()
    async with session() as s:
        rows = (await s.execute(select(Channel))).scalars().all()
    channel_map = {r.tg_chat_id: r.id for r in rows}
    if not channel_map:
        log.warning("no channels registered; add one via `backfill <chat_id>` first")

    @c.on(events.NewMessage(chats=list(channel_map.keys())))
    async def _handler(event):  # pyright: ignore[reportUnusedFunction]
        cid = channel_map.get(event.chat_id)
        if cid is None:
            return
        await _persist(cid, event.message)

    log.info("watching %d channels", len(channel_map))
    await c.run_until_disconnected()


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "backfill":
        chat_id = int(sys.argv[2])
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else None
        asyncio.run(backfill(chat_id, limit))
    elif cmd == "watch":
        asyncio.run(watch())
    else:
        print(f"unknown command: {cmd}")
        sys.exit(2)


if __name__ == "__main__":
    _main()
