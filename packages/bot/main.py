"""aiogram bot entry point.

Owns a *separate* bot token from the fuel-bot (TG Bot API allows only one
polling consumer per token — fuel-bot keeps its own token and keeps
monitoring Gonka escrow undisturbed).
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from packages.bot import ui
from packages.bot.commands import router as commands_router
from packages.bot.register import router as register_router
from packages.bot.settings_cmd import router as settings_router
from packages.common.config import settings

log = logging.getLogger("bot")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    s = settings()
    if not s.tg_bot_token:
        raise RuntimeError("TG_BOT_TOKEN not set")

    bot = Bot(
        token=s.tg_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(register_router)  # my_chat_member → auto-register channels
    dp.include_router(settings_router)  # /settings (owner-only)
    dp.include_router(commands_router)  # /start /summarize /ask /faq /balance

    me = await bot.get_me()
    ui.BOT_USERNAME = me.username or ui.BOT_USERNAME
    log.info("bot online: @%s (id=%s)", me.username, me.id)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
