"""Auto-register channels when the bot is added as an admin.

TG sends a `my_chat_member` update whenever the bot's membership status
in a chat changes.  On `member/administrator` we create a `channels` row
so `/summarize`, `/ask`, etc. can find it.  Also kicks off a backfill
task the first time.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Router
from aiogram.types import ChatMemberUpdated
from sqlalchemy import select

from packages.common.db import session
from packages.common.models import Channel
from packages.context_svc.ingest import backfill

log = logging.getLogger("bot.register")
router = Router()


@router.my_chat_member()
async def on_membership_change(evt: ChatMemberUpdated) -> None:
    status = evt.new_chat_member.status
    chat = evt.chat
    if status not in ("member", "administrator", "creator"):
        # bot was kicked / left / restricted — leave the DB entry alone
        log.info("chat=%s status=%s (ignoring)", chat.id, status)
        return

    title = chat.title or chat.first_name or ""
    # If admin posted anonymously ("send as channel"), from_user is GroupAnonymousBot
    # (id 1087968824) or similar — that's NOT the human owner, don't store it.
    owner_id = None
    if evt.from_user and not evt.from_user.is_bot:
        owner_id = evt.from_user.id
    async with session() as s:
        row = (await s.execute(select(Channel).where(Channel.tg_chat_id == chat.id))).scalar_one_or_none()
        if row is None:
            row = Channel(
                tg_chat_id=chat.id,
                title=title,
                owner_user_id=owner_id,
            )
            s.add(row)
            await s.flush()
            log.info("registered chat=%s title=%r as channels.id=%s", chat.id, title, row.id)
            new_registration = True
        else:
            if title and not row.title:
                row.title = title
            log.info("chat=%s already registered as channels.id=%s", chat.id, row.id)
            new_registration = False

    if new_registration:
        # Kick off history backfill in the background (don't block the handler).
        asyncio.create_task(_safe_backfill(chat.id))


async def _safe_backfill(chat_id: int) -> None:
    try:
        n = await backfill(chat_id)
        log.info("backfill finished: chat=%s messages=%d", chat_id, n)
    except Exception:
        log.exception("backfill failed for chat=%s", chat_id)
