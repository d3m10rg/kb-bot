from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.cleanup import MessageCleanup
from app.config import Settings
from app.handlers import register_handlers
from app.moderation import CaptchaManager
from app.storage import Storage


async def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    storage = Storage(settings.database_path)
    await storage.initialize()
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    cleanup = MessageCleanup(bot, storage)
    bot.session.middleware(cleanup.track_sent_messages)
    captcha = CaptchaManager(bot, storage, settings)
    dispatcher.include_router(register_handlers(captcha, storage, settings, cleanup))

    try:
        await captcha.restore_pending()
        cleanup.start()
        await dispatcher.start_polling(
            bot, allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        await captcha.shutdown()
        await cleanup.shutdown()
        await bot.session.close()


def run() -> None:
    asyncio.run(main())

