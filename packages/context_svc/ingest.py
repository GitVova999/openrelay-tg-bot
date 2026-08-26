"""Pyrogram-based channel ingest — backfill + real-time.

Pyrogram is used (not telethon) so we can reuse the existing jarvis session
already on the VPS.  MTProto user session (not bot token) is required to
pull historical messages for private channels.

Run:
    python -m packages.context_svc.ingest backfill <chat_id> [limit]
    python -m packages.context_svc.ingest watch          # all registered channels
    python -m packages.context_svc.ingest dialogs        # sanity — list first N chats
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime

from pyrogram import Client, filters
from pyrogram.types import Message as PyroMessage
from sqlalchemy import select

from packages.common.config import settings
from packages.common.db import session
from packages.common.models import Channel, Message

log = logging.getLogger("ingest")


def _simhash64(text: str) -> int:
    """Cheap 64-bit SimHash — good enough to catch dup forwards / copy-pastes."""
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


def _msg_text(msg: PyroMessage) -> str:
    return (msg.text or msg.caption or "").strip()


def _sender_name(msg: PyroMessage) -> str:
    u = msg.from_user
    if u is None:
        # Channel-owned message — use chat title as sender.
        return (msg.chat.title if msg.chat else "")[:128]
    parts = [u.first_name or "", u.last_name or ""]
    name = " ".join(p for p in parts if p).strip() or (u.username or f"user:{u.id}")
    return name[:128]


def _client() -> Client:
    s = settings()
    return Client(
        name=s.tg_session_name,
        api_id=s.tg_api_id,
        api_hash=s.tg_api_hash,
        workdir="/opt/openrelay-tg-bot",  # where the .session file lives
    )


async def _ensure_channel(chat_id: int, title: str = "") -> int:
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


async def _persist(channel_id: int, msg: PyroMessage) -> bool:
    text = _msg_text(msg)
    if not text:
        return False
    sent_at: datetime = msg.date if isinstance(msg.date, datetime) else datetime.utcnow()
    row = Message(
        channel_id=channel_id,
        tg_message_id=msg.id,
        sender_user_id=msg.from_user.id if msg.from_user else None,
        sender_name=_sender_name(msg),
        text=text,
        reply_to_tg_id=msg.reply_to_message_id,
        forward_from=(
            (msg.forward_from.first_name if msg.forward_from else None)
            or (msg.forward_from_chat.title if msg.forward_from_chat else None)
        ),
        sent_at=sent_at,
        simhash=_simhash64(text),
    )
    async with session() as s:
        s.add(row)
        try:
            await s.flush()
            return True
        except Exception as e:
            # Duplicate (uq_channel_msg) or transient — ignore.
            log.debug("skip: %s", e)
            await s.rollback()
            return False


async def dialogs(limit: int = 30) -> None:
    """List chats the user is a member of — sanity-check that session is valid
    and that our target chat_id is visible."""
    async with _client() as c:
        me = await c.get_me()
        print(f"logged in as: @{me.username or me.first_name} (id={me.id})")
        n = 0
        async for d in c.get_dialogs(limit=limit):
            print(f"  chat_id={d.chat.id:>18}  type={d.chat.type.name:<15}  title={d.chat.title or d.chat.first_name}")
            n += 1
        print(f"({n} dialogs shown)")


async def backfill(chat_id: int, limit: int | None = None) -> int:
    async with _client() as c:
        entity = await c.get_chat(chat_id)
        title = entity.title or entity.first_name or ""
        internal_id = await _ensure_channel(chat_id, title)
        log.info("backfilling chat=%s (channels.id=%s) title=%r", chat_id, internal_id, title)
        count = 0
        async for msg in c.get_chat_history(chat_id, limit=limit or 0):
            if await _persist(internal_id, msg):
                count += 1
            if count and count % 200 == 0:
                log.info("  … %d messages", count)
        log.info("done: %d messages ingested from chat=%s", count, chat_id)
        return count


async def watch() -> None:
    async with session() as s:
        rows = (await s.execute(select(Channel))).scalars().all()
    channel_map = {r.tg_chat_id: r.id for r in rows}
    if not channel_map:
        log.warning("no channels registered; run `backfill <chat_id>` first")
        return

    c = _client()

    @c.on_message(filters.chat(list(channel_map.keys())))
    async def _handler(_client, msg: PyroMessage):  # pyright: ignore[reportUnusedFunction]
        cid = channel_map.get(msg.chat.id)
        if cid is None:
            return
        await _persist(cid, msg)

    log.info("watching %d channels", len(channel_map))
    await c.start()
    try:
        # idle
        await asyncio.Event().wait()
    finally:
        await c.stop()


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "dialogs":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        asyncio.run(dialogs(limit))
    elif cmd == "backfill":
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
