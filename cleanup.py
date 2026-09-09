from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import NextRequestMiddlewareType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import TelegramMethod
from aiogram.types import Message

from app.storage import Storage

logger = logging.getLogger(__name__)

DELETE_AFTER_SECONDS = 60


class MessageCleanup:
    """Keep deletion deadlines in SQLite, including across bot restarts."""

    def __init__(self, bot: Bot, storage: Storage) -> None:
        self._bot = bot
        self._storage = storage
        self._task: asyncio.Task[None] | None = None

    async def schedule(self, message: Message) -> None:
        await self._storage.schedule_deletion(
            message.chat.id,
            message.message_id,
            message.date.timestamp() + DELETE_AFTER_SECONDS,
        )

    async def track_sent_messages(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Any:
        result = await make_request(bot, method)
        # Edits of captcha messages must not restart their original timer.
        if method.__api_method__.startswith("send"):
            messages = result if isinstance(result, list) else [result]
            for message in messages:
                if isinstance(message, Message):
                    await self.schedule(message)
        return result

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="message-cleanup")

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def delete_due(self) -> None:
        for chat_id, message_id in await self._storage.due_deletions(time.time()):
            try:
                await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
            except (TelegramBadRequest, TelegramForbiddenError):
                logger.warning(
                    "Сообщение %s в чате %s уже удалено или недоступно для удаления",
                    message_id, chat_id,
                )
            # On transient errors the row is retained for the next attempt.
            await self._storage.remove_deletion(chat_id, message_id)

    async def _run(self) -> None:
        while True:
            try:
                await self.delete_due()
            except TelegramRetryAfter as error:
                await asyncio.sleep(error.retry_after)
            except Exception:
                logger.exception("Не удалось выполнить отложенное удаление сообщений")
                await asyncio.sleep(5)
            await asyncio.sleep(1)
