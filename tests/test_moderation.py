import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import ChatMemberRestricted, ChatPermissions, User

from app.config import Settings
from app.moderation import CaptchaManager
from app.storage import Challenge, Storage


def make_settings() -> Settings:
    return Settings(
        bot_token="123:token",
        forbidden_sticker_packs=frozenset(),
        mute_minutes=10,
        captcha_timeout_seconds=60,
        warning_texts=("one", "two", "three"),
        database_path=":memory:",
    )


def make_restricted(user, until_date, **overrides):
    permissions = {name: False for name in ChatMemberRestricted.model_fields if name.startswith("can_")}
    permissions.update(overrides)
    return ChatMemberRestricted(user=user, is_member=True, until_date=until_date, **permissions)


class CaptchaManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database = str(Path(self.temp_dir.name) / "test.sqlite3")
        self.storage = Storage(database)
        await self.storage.initialize()
        self.bot = AsyncMock()
        self.bot.get_chat.return_value = SimpleNamespace(
            permissions=ChatPermissions(can_send_messages=True)
        )
        self.manager = CaptchaManager(self.bot, self.storage, make_settings())

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.temp_dir.cleanup()

    async def add_challenge(self, token: str, answer: int = 7) -> None:
        await self.storage.add_challenge(
            Challenge(
                token=token,
                chat_id=-100,
                user_id=42,
                answer=answer,
                deadline=time.time() + 60,
                message_id=10,
            )
        )

    async def test_only_target_user_can_resolve_challenge(self) -> None:
        await self.add_challenge("foreign")

        result = await self.manager.resolve("foreign", user_id=99, selected=7)

        self.assertEqual(result, "foreign")
        self.assertIsNotNone(await self.storage.get_challenge("foreign"))
        self.bot.restrict_chat_member.assert_not_awaited()

    async def test_correct_answer_restores_chat_permissions(self) -> None:
        await self.add_challenge("correct")

        result = await self.manager.resolve("correct", user_id=42, selected=7)

        self.assertEqual(result, "correct")
        self.bot.restrict_chat_member.assert_awaited_once()
        self.bot.ban_chat_member.assert_not_awaited()
        self.assertIsNone(await self.storage.get_challenge("correct"))

    async def test_wrong_answer_bans_and_immediately_unbans(self) -> None:
        await self.add_challenge("wrong")

        result = await self.manager.resolve("wrong", user_id=42, selected=6)

        self.assertEqual(result, "wrong")
        self.bot.ban_chat_member.assert_awaited_once_with(
            chat_id=-100, user_id=42
        )
        self.bot.unban_chat_member.assert_awaited_once_with(
            chat_id=-100, user_id=42, only_if_banned=True
        )

    async def test_correct_captcha_does_not_cancel_moderation_mute(self):
        await self.add_challenge("muted")
        until = int(time.time()) + 86400
        await self.storage.set_mute(-100, 42, until)
        self.bot.get_chat_member.return_value = make_restricted(
            User(id=42, is_bot=False, first_name="Member"), until
        )
        self.assertEqual(await self.manager.resolve("muted", 42, 7), "correct")
        self.bot.restrict_chat_member.assert_not_awaited()
        self.assertEqual(await self.storage.active_mute(-100, 42), until)

    async def test_captcha_restores_saved_mute_without_making_short_mute_permanent(self):
        await self.add_challenge("short")
        await self.storage.set_mute(-100, 42, time.time() + 10)
        self.bot.get_chat_member.return_value = make_restricted(
            User(id=42, is_bot=False, first_name="Member"), 0
        )
        self.assertEqual(await self.manager.resolve("short", 42, 7), "correct")
        call = self.bot.restrict_chat_member.await_args.kwargs
        self.assertFalse(call["permissions"].can_send_messages)
        self.assertGreaterEqual(call["until_date"], int(time.time()) + 30)
        self.assertLessEqual(call["until_date"], int(time.time()) + 31)

    async def test_captcha_does_not_shorten_a_newer_longer_mute(self):
        await self.add_challenge("longer")
        await self.storage.set_mute(-100, 42, time.time() + 300)
        until = int(time.time()) + 86400
        self.bot.get_chat_member.return_value = make_restricted(
            User(id=42, is_bot=False, first_name="Member"), until
        )
        self.assertEqual(await self.manager.resolve("longer", 42, 7), "correct")
        self.bot.restrict_chat_member.assert_not_awaited()
        self.assertEqual(await self.storage.active_mute(-100, 42), until)


if __name__ == "__main__":
    unittest.main()
