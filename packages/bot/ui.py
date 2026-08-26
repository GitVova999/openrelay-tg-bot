"""Formatting helpers shared across handlers.

Keeps message strings and button layouts out of the command bodies so we can
iterate on wording without touching flow logic.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

BOT_USERNAME = "openrelay_ai_bot"  # updated at runtime via /me on startup if needed


def dm_link(text: str = "Открыть в лс 👉") -> InlineKeyboardMarkup:
    """One-button keyboard that jumps the user into the bot's DM."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, url=f"https://t.me/{BOT_USERNAME}")]]
    )


def deep_link(command: str, arg: str = "") -> InlineKeyboardMarkup:
    """DM link with a deep-link start payload so the bot knows what to run."""
    payload = command if not arg else f"{command}__{arg}"
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Открыть в лс 👉", url=f"https://t.me/{BOT_USERNAME}?start={payload}")
        ]]
    )


WELCOME = (
    "<b>Привет! Я AI-помощник канала.</b>\n\n"
    "Что умею в приватке:\n"
    "• /summarize [часы] — краткое саммари сообщений канала. По умолчанию за последние 24 часа. "
    "Пример: <code>/summarize 168</code> (за неделю)\n"
    "• /ask &lt;вопрос&gt; — отвечаю на любой вопрос, используя контекст канала. "
    "Пример: <code>/ask что обсуждали про Base?</code>\n"
    "• /faq &lt;короткий вопрос&gt; — быстрый ответ (можно и в самом канале)\n"
    "• /balance — баланс кошелька для оплаты запросов\n"
    "• /help — этот список\n\n"
    "В канале/комментах: пиши те же команды, короткий ответ приду сюда, "
    "чтобы не засорять обсуждение."
)

HELP = WELCOME  # keep in sync until they diverge

STUB_ANSWER_IN_DM = (
    "Отправил ответ в личные сообщения 👉 @{bot}\n"
    "<i>Если не пришло — нажми Start у бота, потом повтори команду.</i>"
)

STUB_NEED_START = (
    "Нажми Start у @{bot}, чтобы я мог написать тебе ответ."
)


def stub_answer_in_dm(bot_username: str) -> str:
    return STUB_ANSWER_IN_DM.format(bot=bot_username)


def stub_need_start(bot_username: str) -> str:
    return STUB_NEED_START.format(bot=bot_username)


def format_summary(res: dict) -> str:
    """Turn summarize_channel(...) result into a nice HTML message.

    We escape the model's output so any residual HTML-looking bits in the
    summary don't crash the aiogram HTML parser.
    """
    from packages.context_svc.sanitize import clean_llm_output
    header = (
        f"<b>📝 Саммари</b> · {res['msg_count']} сообщ. · {res['period']}\n"
        f"<i>Модель: {res['model']} · {res['in_tokens']}→{res['out_tokens']} tok</i>\n"
    )
    return header + "\n" + clean_llm_output(res["summary"])


def truncate_for_telegram(text: str, limit: int = 4000) -> list[str]:
    """TG hard-caps a single message at 4096 chars. Split cleanly at paragraphs."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        add = ("\n\n" if buf else "") + para
        if len(buf) + len(add) > limit:
            if buf:
                chunks.append(buf)
            # If one paragraph is itself over limit, hard-slice it.
            while len(para) > limit:
                chunks.append(para[:limit])
                para = para[limit:]
            buf = para
        else:
            buf += add
    if buf:
        chunks.append(buf)
    return chunks
