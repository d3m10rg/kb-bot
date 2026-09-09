import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import DeleteMessage, SendMessage
from aiogram.types import Chat, Message, User

from app.cleanup import MessageCleanup
from app.moderation import CaptchaManager
from app.storage import Storage
from tests.test_moderation import make_settings


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp_dir.name) / "test.sqlite3")
        self.storage = Storage(self.path)
        await self.storage.initialize()
        self.bot = AsyncMock()
        self.cleanup = MessageCleanup(self.bot, self.storage)
        self.message = Message(message_id=10, chat=Chat(id=-100, type="supergroup"), date=1000)

    async def asyncTearDown(self):
        await self.cleanup.shutdown()
        self.temp_dir.cleanup()

    async def test_deadline_survives_restart_and_deletes_only_when_due(self):
        await self.cleanup.schedule(self.message)
        reopened = Storage(self.path)
        await reopened.initialize()
        cleanup = MessageCleanup(self.bot, reopened)
        with patch("app.cleanup.time.time", return_value=1059):
            await cleanup.delete_due()
        self.bot.delete_message.assert_not_awaited()
        with patch("app.cleanup.time.time", return_value=1060):
            await cleanup.delete_due()
        self.bot.delete_message.assert_awaited_once_with(chat_id=-100, message_id=10)
        self.assertEqual(await reopened.due_deletions(2000), [])

    async def test_deleted_message_does_not_stop_other_deletions(self):
        await self.cleanup.schedule(self.message)
        await self.storage.schedule_deletion(-100, 11, 1000)
        self.bot.delete_message.side_effect = [
            TelegramBadRequest(method=DeleteMessage(chat_id=-100, message_id=11), message="not found"),
            True,
        ]
        with self.assertLogs("app.cleanup", level="WARNING"):
            await self.cleanup.delete_due()
        self.assertEqual(self.bot.delete_message.await_count, 2)
        self.assertEqual(await self.storage.due_deletions(time.time()), [])

    async def test_network_failure_retains_job_for_retry(self):
        await self.cleanup.schedule(self.message)
        self.bot.delete_message.side_effect = TelegramNetworkError(
            method=DeleteMessage(chat_id=-100, message_id=10), message="offline"
        )
        with self.assertRaises(TelegramNetworkError):
            await self.cleanup.delete_due()
        self.assertEqual(await self.storage.due_deletions(time.time()), [(-100, 10)])
        self.bot.delete_message.side_effect = None
        await self.cleanup.delete_due()
        self.assertEqual(await self.storage.due_deletions(time.time()), [])

    async def test_real_bot_session_tracks_sends_but_not_edits(self):
        bot = Bot("123:token")
        try:
            bot.session.make_request = AsyncMock(return_value=self.message)
            bot.session.middleware(self.cleanup.track_sent_messages)
            await bot.send_message(-100, "hello")
            await bot.edit_message_text(chat_id=-100, message_id=10, text="edited")
            self.assertEqual(await self.storage.due_deletions(1059), [])
            self.assertEqual(await self.storage.due_deletions(1060), [(-100, 10)])
            await self.storage.remove_deletion(-100, 10)
            await bot.edit_message_text(chat_id=-100, message_id=10, text="again")
            self.assertEqual(await self.storage.due_deletions(2000), [])
        finally:
            await bot.session.close()

    async def test_captcha_message_gets_same_cleanup_and_keyboard_deadline(self):
        bot = Bot("123:token")
        cleanup = MessageCleanup(bot, self.storage)
        settings = make_settings()
        manager = CaptchaManager(bot, self.storage, replace(settings, captcha_timeout_seconds=120))

        async def request(bot, method, **kwargs):
            if isinstance(method, SendMessage):
                return self.message.model_copy(update={"date": self.now})
            return True

        self.now = datetime.now(timezone.utc)
        bot.session.make_request = AsyncMock(side_effect=request)
        bot.session.middleware(cleanup.track_sent_messages)
        try:
            await manager.start(-100, User(id=42, first_name="Member", is_bot=False))
            challenge, = await self.storage.list_challenges()
            self.assertAlmostEqual(challenge.deadline, time.time() + 60, delta=2)
            self.assertEqual(await self.storage.due_deletions(time.time() + 61), [(-100, 10)])
        finally:
            await manager.shutdown()
            await bot.session.close()

    async def test_shutdown_keeps_pending_rows_for_restart(self):
        message = self.message.model_copy(update={"date": self.message.date.replace(year=2099)})
        await self.cleanup.schedule(message)
        self.cleanup.start()
        await self.cleanup.shutdown()
        self.assertEqual(await self.storage.due_deletions(message.date.timestamp() + 61), [(-100, 10)])
