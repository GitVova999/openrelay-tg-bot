"""/settings — inspect and edit per-channel prompt config.

Owner-only.  Editable fields: language, tone, topics, system_prompt (raw override).

Usage in DM:
    /settings                       show current config
    /settings language ru           set language to ru
    /settings tone "дерзко, с иронией"
    /settings topics "крипта, геополитика, философия"
    /settings system_prompt "..."   full override
    /settings reset system_prompt   drop override
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from packages.bot import ui
from packages.common.db import session
from packages.common.models import Channel
from packages.context_svc.sanitize import clean_llm_output

log = logging.getLogger("bot.settings")
router = Router()

_EDITABLE = {"language", "tone", "topics", "system_prompt"}


def _fmt_channel(ch: Channel) -> str:
    override = "<b>(override)</b>" if ch.system_prompt else "(auto)"
    sp = ch.system_prompt or "(генерируется из language/tone/topics)"
    return (
        f"<b>⚙️ Настройки канала «{clean_llm_output(ch.title)}»</b>\n"
        f"<code>tg_chat_id: {ch.tg_chat_id}</code>\n\n"
        f"<b>language:</b> <code>{ch.language}</code>\n"
        f"<b>tone:</b> <code>{clean_llm_output(ch.tone) or '(не задан)'}</code>\n"
        f"<b>topics:</b> <code>{clean_llm_output(ch.topics) or '(не заданы)'}</code>\n"
        f"<b>system_prompt:</b> {override}\n"
        f"<pre>{clean_llm_output(sp)[:600]}</pre>\n\n"
        "<i>Изменить: /settings &lt;field&gt; &lt;value&gt;.\n"
        "Поля: language | tone | topics | system_prompt.\n"
        "Сброс override: /settings reset system_prompt</i>"
    )


@router.message(Command("settings"), lambda m: m.chat.type == ChatType.PRIVATE)
async def settings_cmd(message: Message, command) -> None:  # type: ignore[no-untyped-def]
    if message.from_user is None:
        return
    args = (command.args or "").strip()

    async with session() as db:
        # Owner must own at least one channel.
        rows = (await db.execute(
            select(Channel).where(Channel.owner_user_id == message.from_user.id)
        )).scalars().all()

    if not rows:
        await message.answer(
            "Ты пока не владелец ни одного канала в базе.  "
            "Добавь меня админом в канал — я его зарегистрирую на тебя."
        )
        return

    # MVP: single owned channel → edit it. Multi-owned: show list, ask to pick.
    if len(rows) > 1:
        listing = "\n".join(f"• <code>{r.tg_chat_id}</code> {clean_llm_output(r.title)}" for r in rows)
        await message.answer(
            f"У тебя несколько каналов, выбор пока не реализован:\n{listing}\n\n"
            "Скажи мне какой редактируем — /settings ждёт per-channel selector "
            "(добавлю по запросу)."
        )
        return
    ch = rows[0]

    if not args:
        await message.answer(_fmt_channel(ch))
        return

    parts = args.split(None, 1)
    verb = parts[0].lower()

    if verb == "reset":
        field = parts[1].strip().lower() if len(parts) > 1 else ""
        if field not in _EDITABLE:
            await message.answer(f"Поле должно быть одним из: {', '.join(_EDITABLE)}")
            return
        async with session() as db:
            row = (await db.execute(select(Channel).where(Channel.id == ch.id))).scalar_one()
            setattr(row, field, "" if field != "system_prompt" else None)
            if field == "language":
                row.language = "ru"
        await message.answer(f"OK, {field} сброшено.")
        return

    if verb not in _EDITABLE:
        await message.answer(f"Не знаю поле <code>{verb}</code>. Список: {', '.join(_EDITABLE)}")
        return
    if len(parts) < 2:
        await message.answer(f"Нужно значение: /settings {verb} &lt;value&gt;")
        return
    value = parts[1].strip().strip('"').strip("'")

    if verb == "language" and value.lower() not in ("ru", "en", "es"):
        await message.answer("Поддерживаемые языки: ru, en, es.")
        return

    async with session() as db:
        row = (await db.execute(select(Channel).where(Channel.id == ch.id))).scalar_one()
        setattr(row, verb, value)
    await message.answer(f"OK, {verb} = <code>{clean_llm_output(value)[:200]}</code>")
