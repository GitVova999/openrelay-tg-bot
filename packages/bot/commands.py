"""Command handlers — dual-handler (private vs channel/group).

Design:
- In DM: full answers, no length gymnastics.
- In channel/group/comment thread: short public stub + inline button that
  jumps the caller into the bot's DM, where the full answer arrives.
- If the caller hasn't /start'd the bot yet, we can't DM them; we tell them
  to press Start (only way TG allows initiating DM to a user).

`ask` and `summarize` deliberately share the "route to DM" pattern — the
answer is always big enough to warrant a private thread, and users don't
want their questions/answers cluttering the channel.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import select

from packages.bot import ui
from packages.common.db import session
from packages.common.models import Channel
from packages.context_svc.openrelay_client import chat_completion
from packages.context_svc.summarize import (
    _fetch_window,
    _format_transcript,
    summarize_channel,
)

log = logging.getLogger("bot.commands")
router = Router()

PRIVATE = ChatType.PRIVATE
NON_PRIVATE = {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}


# ─────────────────────────────────────────────────────────── /start & /help


@router.message(CommandStart(deep_link=True))
async def start_with_payload(message: Message, command) -> None:  # type: ignore[no-untyped-def]
    """Deep-link start: /start with an arg like `summarize__24` or `ask__какой%20курс`.
    Runs the encoded command immediately so users landing from a channel button
    don't have to retype what they asked."""
    payload = (command.args or "").strip()
    await message.answer(ui.WELCOME)
    if not payload:
        return
    parts = payload.split("__", 1)
    verb = parts[0]
    arg = parts[1] if len(parts) > 1 else ""
    if verb == "summarize":
        hours = int(arg) if arg.isdigit() else 24
        await _do_summarize(message, hours=hours)
    elif verb == "ask":
        await _do_ask(message, question=arg)


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(ui.WELCOME)


@router.message(Command("help"))
async def help_(message: Message) -> None:
    await message.answer(ui.HELP)


# ─────────────────────────────────────────────────────────── /summarize


@router.message(Command("summarize"), F.chat.type == PRIVATE)
async def summarize_private(message: Message, command) -> None:  # type: ignore[no-untyped-def]
    hours = _parse_hours(command.args)
    await _do_summarize(message, hours=hours, source_chat_id=None)


@router.message(Command("summarize"), F.chat.type.in_(NON_PRIVATE))
async def summarize_public(message: Message, command, bot: Bot) -> None:  # type: ignore[no-untyped-def]
    hours = _parse_hours(command.args)
    await _stub_and_relay_to_dm(
        message, bot,
        verb="summarize", arg=str(hours),
        run=lambda msg: _do_summarize(msg, hours=hours, source_chat_id=message.chat.id),
    )


async def _do_summarize(msg: Message, hours: int, source_chat_id: int | None = None) -> None:
    """Actual summarization work — always runs against the CHANNEL's data.

    `source_chat_id` = if the command came from within a channel, use that.
    Otherwise DM — user must specify or we default to their only registered channel.
    """
    chat_id = source_chat_id or await _resolve_default_channel(msg.from_user.id if msg.from_user else 0)
    if chat_id is None:
        await msg.answer(
            "Не понял, для какого канала. Добавь меня админом в канал — я его зарегистрирую, "
            "потом пиши /summarize в самом канале или тут в лс."
        )
        return

    thinking = await msg.answer(f"⏳ Собираю сводку за {hours}ч…")
    try:
        res = await summarize_channel(chat_id, hours=hours)
    except Exception as e:
        await thinking.edit_text(f"⚠️ Ошибка: <code>{e!s}</code>")
        return
    if not res["ok"]:
        await thinking.edit_text(f"⚠️ {res['error']}")
        return
    text = ui.format_summary(res)
    chunks = ui.truncate_for_telegram(text)
    await thinking.edit_text(chunks[0])
    for extra in chunks[1:]:
        await msg.answer(extra)


# ─────────────────────────────────────────────────────────── /ask


@router.message(Command("ask"), F.chat.type == PRIVATE)
async def ask_private(message: Message, command) -> None:  # type: ignore[no-untyped-def]
    q = (command.args or "").strip()
    if not q:
        await message.answer("Пример: <code>/ask о чём был последний спор?</code>")
        return
    await _do_ask(message, question=q)


@router.message(Command("ask"), F.chat.type.in_(NON_PRIVATE))
async def ask_public(message: Message, command, bot: Bot) -> None:  # type: ignore[no-untyped-def]
    q = (command.args or "").strip()
    if not q:
        await message.reply("Пример: <code>/ask о чём был последний спор?</code>")
        return
    await _stub_and_relay_to_dm(
        message, bot,
        verb="ask", arg=q,
        run=lambda msg: _do_ask(msg, question=q, source_chat_id=message.chat.id),
    )


async def _do_ask(msg: Message, question: str, source_chat_id: int | None = None) -> None:
    """Answer a free-form question grounded in the channel's messages.

    MVP: concatenate whole corpus (fits for <100k tok channels; retrieval
    layer lands in milestone 2). Response is streamed as one block for now
    — token-by-token streaming is a v0.2 improvement.
    """
    chat_id = source_chat_id or await _resolve_default_channel(msg.from_user.id if msg.from_user else 0)
    if chat_id is None:
        await msg.answer("Нет зарегистрированного канала. Сначала добавь меня в канал как админа.")
        return

    thinking = await msg.answer("⏳ Думаю…")
    async with session() as db:
        ch = (await db.execute(select(Channel).where(Channel.tg_chat_id == chat_id))).scalar_one_or_none()
    if ch is None:
        await thinking.edit_text("Канал не зарегистрирован в базе.")
        return
    msgs = await _fetch_window(ch.id, since=None, limit=None)
    if not msgs:
        await thinking.edit_text("В канале пока нет распарсенных сообщений.")
        return

    transcript = _format_transcript(msgs)
    prompt = (
        f"Ниже — весь публичный контекст канала «{ch.title}» "
        f"({len(msgs)} сообщ.). Ответь на вопрос пользователя, "
        "опираясь ТОЛЬКО на этот контекст. Если ответа нет — так и скажи. "
        "Отвечай на русском, кратко и по делу.\n\n"
        f"КОНТЕКСТ:\n===\n{transcript}\n===\n\n"
        f"ВОПРОС: {question}"
    )
    try:
        j = await chat_completion(
            model="deepseek-ai/DeepSeek-V4-Flash-0731",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
            temperature=0.4,
        )
    except Exception as e:
        await thinking.edit_text(f"⚠️ Инференс упал: <code>{e!s}</code>")
        return

    answer = j["choices"][0]["message"]["content"]
    usage = j.get("usage", {})
    header = f"<b>❓ {question}</b>\n<i>{usage.get('prompt_tokens', 0)}→{usage.get('completion_tokens', 0)} tok</i>\n\n"
    chunks = ui.truncate_for_telegram(header + answer)
    await thinking.edit_text(chunks[0])
    for extra in chunks[1:]:
        await msg.answer(extra)


# ─────────────────────────────────────────────────────────── /faq


@router.message(Command("faq"))
async def faq(message: Message, command) -> None:  # type: ignore[no-untyped-def]
    """FAQ = one-line answer, safe to run inline in a channel.

    Uses a small model, tight token cap, low temp. If the answer clearly
    exceeds one message worth of content, we truncate + offer "continue in DM".
    """
    q = (command.args or "").strip()
    if not q:
        await message.reply("Пример: <code>/faq что такое x402?</code>")
        return
    thinking = await message.reply("⏳")
    try:
        j = await chat_completion(
            model="deepseek-ai/DeepSeek-V4-Flash-0731",
            messages=[{
                "role": "user",
                "content": (
                    "Ответь на вопрос одной-двумя фразами. "
                    "Максимум 40 слов. Только суть. Русский язык.\n\n"
                    f"Вопрос: {q}"
                ),
            }],
            max_tokens=120,
            temperature=0.2,
        )
    except Exception as e:
        await thinking.edit_text(f"⚠️ <code>{e!s}</code>")
        return
    ans = j["choices"][0]["message"]["content"].strip()
    kb = ui.deep_link("ask", q) if message.chat.type != PRIVATE else None
    await thinking.edit_text(f"<b>❓ {q}</b>\n{ans}", reply_markup=kb)


# ─────────────────────────────────────────────────────────── /balance


@router.message(Command("balance"))
async def balance(message: Message) -> None:
    import httpx
    from eth_account import Account
    import os
    key = os.environ.get("OPENRELAY_WALLET_PRIVATE_KEY", "")
    if not key:
        await message.answer("Кошелёк не сконфигурирован.")
        return
    if not key.startswith("0x"):
        key = "0x" + key
    addr = Account.from_key(key).address
    async with httpx.AsyncClient(timeout=10) as h:
        r = await h.post(
            "https://mainnet.base.org",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{
                    "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "data": f"0x70a08231{'0'*24}{addr[2:].lower()}",
                }, "latest"],
            },
        )
    balance_micro = int(r.json()["result"], 16)
    usd = balance_micro / 1e6
    await message.answer(
        f"💰 <b>Кошелёк бота</b>\n"
        f"<code>{addr}</code>\n"
        f"Base USDC: <b>${usd:.4f}</b>\n\n"
        f"Хватает примерно на <b>{int(usd / 0.01)}</b> запросов (CDP min $0.01/req)."
    )


# ─────────────────────────────────────────────────────────── helpers


def _parse_hours(args: str | None) -> int:
    if not args:
        return 24
    m = re.match(r"^\s*(\d+)", args)
    return int(m.group(1)) if m else 24


async def _resolve_default_channel(_user_id: int) -> int | None:
    """MVP: return the only registered channel if there's exactly one.

    Later: per-user "current channel" state, or force user to specify.
    """
    async with session() as s:
        rows = (await s.execute(select(Channel))).scalars().all()
    if len(rows) == 1:
        return rows[0].tg_chat_id
    return None


async def _stub_and_relay_to_dm(
    channel_msg: Message,
    bot: Bot,
    *,
    verb: str,
    arg: str,
    run,  # Callable[[Message], Awaitable[None]]
) -> None:
    """Public stub + fire-and-forget DM. If user hasn't /start'd the bot,
    we can't DM them; the stub falls back to a deep-link Start button."""
    me = await bot.get_me()
    user = channel_msg.from_user
    if user is None:
        await channel_msg.reply("Не могу определить пользователя.")
        return

    # Try to DM first — if it fails, we know user hasn't Start'd.
    try:
        dm_msg = await bot.send_message(user.id, f"⏳ обрабатываю /{verb}…")
    except TelegramForbiddenError:
        await channel_msg.reply(
            ui.stub_need_start(me.username),
            reply_markup=ui.deep_link(verb, arg),
        )
        return

    # Confirm in channel with a link to the DM.
    await channel_msg.reply(ui.stub_answer_in_dm(me.username), reply_markup=ui.dm_link())

    # Run the real work in the DM message context.
    try:
        await run(dm_msg)
    except Exception as e:
        log.exception("dm run failed")
        await bot.send_message(user.id, f"⚠️ Не смог выполнить: <code>{e!s}</code>")
