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

import asyncio
import logging
import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import func, select

from packages.bot import ui
from packages.common.db import session
from packages.common.models import Channel, Message as DbMessage
from packages.context_svc.digest import build_context_for_query, ensure_digests
from packages.context_svc.openrelay_client import chat_completion
from packages.context_svc.prompts import STOP_SEQUENCES, build_system_prompt
from packages.context_svc.sanitize import clean_llm_output
from packages.context_svc.summarize import summarize_channel

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
        await _do_ask_in_dm(message, question=arg)


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
        await thinking.edit_text(f"⚠️ Ошибка: <code>{clean_llm_output(str(e))}</code>")
        return
    if not res["ok"]:
        await thinking.edit_text(f"⚠️ {clean_llm_output(res['error'])}")
        return
    # Pick channel language for smart reasoning-strip in summary render.
    async with session() as db:
        _ch = (await db.execute(select(Channel).where(Channel.tg_chat_id == chat_id))).scalar_one_or_none()
    lang = (_ch.language if _ch else "ru")
    text = ui.format_summary(res, language=lang)
    chunks = ui.truncate_for_telegram(text)
    await thinking.edit_text(chunks[0])
    for extra in chunks[1:]:
        await msg.answer(extra)


# ─────────────────────────────────────────────────────────── /ask


# Threshold below which an /ask answer stays inline in the channel/group.
# Long answers still route to DM to keep public chat tidy.  Chosen so a
# ~3-line one-paragraph answer fits without truncation warnings.
CHANNEL_INLINE_LIMIT_CHARS = 400


@router.message(Command("ask"), F.chat.type == PRIVATE)
async def ask_private(message: Message, command) -> None:  # type: ignore[no-untyped-def]
    q = (command.args or "").strip()
    reply_focus = _extract_reply_focus(message)
    if not q and not reply_focus:
        await message.answer(
            "Пример: <code>/ask о чём был последний спор?</code>\n"
            "Или ответь на пост командой <code>/ask</code> — я поясню его."
        )
        return
    if not q:
        q = "О чём этот пост? Прокомментируй."
    await _do_ask_in_dm(message, question=q, reply_focus=reply_focus)


@router.message(Command("ask"), F.chat.type.in_(NON_PRIVATE))
async def ask_public(message: Message, command, bot: Bot) -> None:  # type: ignore[no-untyped-def]
    q = (command.args or "").strip()
    reply_focus = _extract_reply_focus(message)
    if not q and not reply_focus:
        await message.reply(
            "Пример: <code>/ask о чём был последний спор?</code>\n"
            "Или ответь на пост командой <code>/ask</code> — я поясню его."
        )
        return
    if not q:
        q = "О чём этот пост? Прокомментируй."
    await _do_ask_in_channel(message, bot, question=q, reply_focus=reply_focus)


def _extract_reply_focus(message: Message) -> str | None:
    """If /ask was sent as a reply to another message, return that message's
    text/caption in a formatted 'target post' block, or None."""
    r = message.reply_to_message
    if r is None:
        return None
    text = (r.text or r.caption or "").strip()
    if not text:
        return None
    who = ""
    if r.from_user:
        who = r.from_user.username or r.from_user.first_name or ""
    elif r.sender_chat:
        who = r.sender_chat.title or ""
    stamp = r.date.strftime("%Y-%m-%d %H:%M") if r.date else "?"
    header = f"[{stamp}]" + (f" {who}:" if who else "")
    return f"{header}\n{text}"


async def _generate_ask_answer(
    channel_tg_id: int,
    question: str,
    reply_focus: str | None = None,
) -> dict:
    """Run the full /ask pipeline; return {ok, header, body, chunks, lang}.

    Kept separate from the send layer so both DM and channel handlers can
    format-and-route the same result without duplicating the LLM call.
    """
    async with session() as db:
        ch = (await db.execute(
            select(Channel).where(Channel.tg_chat_id == channel_tg_id)
        )).scalar_one_or_none()
    if ch is None:
        return {"ok": False, "error": "Канал не зарегистрирован в базе."}

    context, stats = await build_context_for_query(ch.id, recent_hours=48)
    if not context:
        return {"ok": False, "error": "В канале пока нет распарсенных сообщений."}

    # Fire background digest warm (bounded per-call) so next /ask has more.
    asyncio.create_task(_background_digest_warm(ch.id, max_new=5))

    focus_block = ""
    if reply_focus:
        focus_block = (
            "ПОСТ-АНКОР (пользователь спрашивает про этот конкретный пост, "
            "используй его как основной фокус ответа):\n"
            f"===\n{reply_focus}\n===\n\n"
        )

    user_prompt = (
        f"Ниже — контекст канала: {stats['raw_msgs']} свежих сообщ. за 48ч "
        f"+ {stats['digest_days']} дневных дайджестов старше 48ч.\n"
        + (focus_block if focus_block else "")
        + "Ответь на вопрос пользователя, опираясь на пост-анкор (если есть) "
          "и контекст канала. Если ответа нет — так и скажи. Кратко, по делу.\n\n"
        f"КОНТЕКСТ:\n{context}\n\n"
        f"ВОПРОС: {question}"
    )
    system_prompt = build_system_prompt(ch, task="assistant")

    try:
        j = await chat_completion(
            model="deepseek-ai/DeepSeek-V4-Flash-0731",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=1500,
            temperature=0.4,
            stop=STOP_SEQUENCES,
        )
    except Exception as e:
        return {"ok": False, "error": f"Инференс упал: <code>{clean_llm_output(str(e))}</code>"}

    raw = j["choices"][0]["message"]["content"]
    lang = (ch.language or "ru").lower()
    answer = clean_llm_output(raw, expected_lang=lang)
    if not answer:
        answer = "(модель вернула пустой ответ после reasoning — переформулируй)"
    usage = j.get("usage", {})
    header = (
        f"<b>❓ {clean_llm_output(question, expected_lang=lang)}</b>\n"
        f"<i>{usage.get('prompt_tokens', 0)}→{usage.get('completion_tokens', 0)} tok · "
        f"raw:{stats['raw_msgs']} digests:{stats['digest_days']} · "
        f"{j.get('model', '?')}</i>\n\n"
    )
    return {
        "ok": True,
        "header": header,
        "body": answer,
        "chunks": ui.truncate_for_telegram(header + answer),
        "lang": lang,
    }


async def _do_ask_in_dm(
    msg: Message,
    question: str,
    source_chat_id: int | None = None,
    reply_focus: str | None = None,
) -> None:
    """DM path — always full answer regardless of length."""
    chat_id = source_chat_id or await _resolve_default_channel(msg.from_user.id if msg.from_user else 0)
    if chat_id is None:
        await msg.answer("Нет зарегистрированного канала. Сначала добавь меня в канал как админа.")
        return

    thinking = await msg.answer("⏳ Думаю…")
    res = await _generate_ask_answer(chat_id, question, reply_focus=reply_focus)
    if not res["ok"]:
        await thinking.edit_text(f"⚠️ {res['error']}")
        return
    await thinking.edit_text(res["chunks"][0])
    for extra in res["chunks"][1:]:
        await msg.answer(extra)


async def _do_ask_in_channel(
    channel_msg: Message,
    bot: Bot,
    question: str,
    reply_focus: str | None = None,
) -> None:
    """Channel/group path.

    - Short answer (≤ CHANNEL_INLINE_LIMIT_CHARS): reply right there in the
      channel — no reason to force a DM detour for a two-sentence answer.
    - Long answer: post a short stub in the channel + full answer to the
      user's DM.  If the user is an anonymous-admin (from_user.is_bot) or
      never pressed /start, replace the stub with a deep-link they can
      tap to open DM pre-filled with the same question.
    """
    thinking = await channel_msg.reply("⏳ Думаю…")
    res = await _generate_ask_answer(channel_msg.chat.id, question, reply_focus=reply_focus)
    if not res["ok"]:
        await thinking.edit_text(f"⚠️ {res['error']}")
        return

    # `body` excludes the meta header — length threshold uses the pure answer.
    body_len = len(res["body"])
    if body_len <= CHANNEL_INLINE_LIMIT_CHARS:
        # Short — stays inline. Drop the metadata header for a cleaner look.
        await thinking.edit_text(res["body"])
        return

    # Long — route to DM. Deal with the "can't DM" cases first.
    me = await bot.get_me()
    user = channel_msg.from_user
    if user is None or user.is_bot:
        await thinking.edit_text(
            "Ответ длинный. Открой в лс — я пришлю туда полный:",
            reply_markup=ui.deep_link("ask", question),
        )
        return

    try:
        # Full answer to DM (may span multiple messages if > 4 KB).
        for chunk in res["chunks"]:
            await bot.send_message(user.id, chunk)
    except (TelegramForbiddenError, TelegramBadRequest):
        await thinking.edit_text(
            ui.stub_need_start(me.username),
            reply_markup=ui.deep_link("ask", question),
        )
        return

    await thinking.edit_text(
        ui.stub_answer_in_dm(me.username),
        reply_markup=ui.dm_link(),
    )


async def _background_digest_warm(channel_pk: int, max_new: int = 5) -> None:
    try:
        n = await ensure_digests(channel_pk, max_new=max_new)
        if n:
            log.info("background digest warm: channel=%s +%d", channel_pk, n)
    except Exception:
        log.exception("background digest warm failed")


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
    # Resolve channel to pick up per-channel language setting (falls back to ru).
    chat_id = message.chat.id if message.chat.type != PRIVATE else \
              await _resolve_default_channel(message.from_user.id if message.from_user else 0)
    async with session() as db:
        ch = (await db.execute(select(Channel).where(Channel.tg_chat_id == chat_id))).scalar_one_or_none() \
             if chat_id else None
    system_prompt = build_system_prompt(ch, task="faq") if ch else (
        "Ты помощник. Отвечай на русском. Никаких <think>-блоков."
    )
    try:
        j = await chat_completion(
            model="deepseek-ai/DeepSeek-V4-Flash-0731",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": (
                    "Ответь на вопрос одной-двумя фразами. Максимум 40 слов. Только суть.\n\n"
                    f"Вопрос: {q}"
                )},
            ],
            max_tokens=300,
            temperature=0.2,
            stop=STOP_SEQUENCES,
        )
    except Exception as e:
        await thinking.edit_text(f"⚠️ <code>{clean_llm_output(str(e))}</code>")
        return
    lang = (ch.language or "ru").lower() if ch else "ru"
    ans = clean_llm_output(j["choices"][0]["message"]["content"].strip(), expected_lang=lang)
    kb = ui.deep_link("ask", q) if message.chat.type != PRIVATE else None
    await thinking.edit_text(
        f"<b>❓ {clean_llm_output(q, expected_lang=lang)}</b>\n{ans}",
        reply_markup=kb,
    )


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


async def _resolve_default_channel(user_id: int) -> int | None:
    """Pick a default channel for a DM command.

    Priority: (1) channels this user owns, (2) channel with the most messages
    (i.e. the one with real content to summarize/ask), (3) None.  MVP — a
    proper per-user "current channel" selector lands with the docker tier.
    """
    async with session() as s:
        # 1. Owned by this user
        if user_id:
            owned = (await s.execute(
                select(Channel).where(Channel.owner_user_id == user_id)
            )).scalars().first()
            if owned is not None:
                return owned.tg_chat_id

        # 2. Fall back to whichever channel has the most ingested messages.
        stmt = (
            select(Channel.tg_chat_id)
            .join(DbMessage, DbMessage.channel_id == Channel.id, isouter=True)
            .group_by(Channel.id)
            .order_by(func.count(DbMessage.id).desc())
            .limit(1)
        )
        row = (await s.execute(stmt)).first()
        return row[0] if row else None


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

    # Anonymous-admin posts arrive as GroupAnonymousBot — TG blocks bot-to-bot
    # DMs, so we can't route the answer that way.  Give them a deep-link so
    # they can run the same command in DM from their own account.
    if user.is_bot:
        await channel_msg.reply(
            "Ты пишешь от имени канала/анонимно, я не могу ответить в лс "
            "боту. Открой ЛС со мной со своего аккаунта и повтори:",
            reply_markup=ui.deep_link(verb, arg),
        )
        return

    # Try to DM first — if it fails, we know user hasn't Start'd.
    try:
        dm_msg = await bot.send_message(user.id, f"⏳ обрабатываю /{verb}…")
    except (TelegramForbiddenError, TelegramBadRequest):
        # TelegramForbiddenError = user blocked / never started;
        # TelegramBadRequest fires on "chat not found" for the same reason.
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
