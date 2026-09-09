import asyncio
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import DeleteMessage, GetChatMember, GetMe, RestrictChatMember, SendMessage
from aiogram.types import Chat, ChatMemberMember, ChatMemberOwner, Message, Update, User

from app.cleanup import MessageCleanup
from app.handlers import register_handlers
from app.storage import Storage
from tests.test_moderation import make_settings


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.temp_dir.name) / "test.sqlite3"))
        await self.storage.initialize()
        self.bot = Bot("123:token")
        self.admin = User(id=1, is_bot=False, first_name="Admin", username="admin")
        self.user = User(id=42, is_bot=False, first_name="Member", username="member")
        self.bot_user = User(id=123, is_bot=True, first_name="Bot", username="kb_test_bot")
        self.chat = Chat(id=-100, type="supergroup")
        self.members = {
            1: ChatMemberOwner(user=self.admin, is_anonymous=False),
            42: ChatMemberMember(user=self.user),
            123: ChatMemberMember(user=self.bot_user),
        }
        self.sent = []
        self.mutes = []
        self.deny_mute = False
        self.bot.session.make_request = AsyncMock(side_effect=self.request)
        self.cleanup = MessageCleanup(self.bot, self.storage)
        self.bot.session.middleware(self.cleanup.track_sent_messages)
        self.dispatcher = Dispatcher()
        self.captcha = AsyncMock()
        self.dispatcher.include_router(
            register_handlers(
                self.captcha, self.storage,
                replace(make_settings(), forbidden_sticker_packs=frozenset({"blocked"})),
                self.cleanup,
            )
        )
        self.sequence = 0

    async def asyncTearDown(self) -> None:
        await self.cleanup.shutdown()
        await self.bot.session.close()
        self.temp_dir.cleanup()

    async def request(self, bot, method, **kwargs):
        if isinstance(method, GetMe):
            return self.bot_user
        if isinstance(method, GetChatMember):
            return self.members[method.user_id]
        if isinstance(method, SendMessage):
            sent = Message(
                message_id=1000 + len(self.sent), chat=self.chat,
                date=datetime.now(timezone.utc), text=method.text, from_user=self.bot_user,
            )
            self.sent.append(sent)
            return sent
        if isinstance(method, RestrictChatMember):
            if self.deny_mute:
                raise TelegramForbiddenError(method=method, message="no rights")
            self.mutes.append(method)
            self.members[method.user_id] = SimpleNamespace(
                status="restricted", user=self.members[method.user_id].user,
                is_member=True, can_send_messages=False, until_date=method.until_date,
            )
            return True
        return True

    def message(self, user=None, text=None, **kwargs):
        self.sequence += 1
        fields = dict(
            message_id=self.sequence, chat=self.chat,
            date=int(time.time()), from_user=user or self.user,
            text=text,
        )
        if text and text.startswith("/"):
            fields["entities"] = [
                dict(type="bot_command", offset=0, length=len(text.split()[0]))
            ]
        fields.update(kwargs)
        return Message(**fields)

    async def feed(self, user=None, text=None, **kwargs):
        message = self.message(user, text, **kwargs)
        await self.dispatcher.feed_update(
            self.bot, Update(update_id=self.sequence, message=message)
        )
        return message

    async def test_three_admin_warnings_mute_for_a_day_and_keep_count(self):
        await self.feed(text="hello")
        for count in range(1, 4):
            await self.feed(self.admin, "/warn @MeMbEr")
            self.assertEqual(
                self.sent[-1].text,
                f"Пользователю @member дано предупреждение. Количество предупреждений: {count}",
            )
            self.assertEqual(len(self.mutes), int(count == 3))
        self.assertAlmostEqual(self.mutes[0].until_date.timestamp(), time.time() + 86400, delta=3)
        self.assertTrue(self.mutes[0].use_independent_chat_permissions)
        self.assertTrue(all(
            value is False for name, value in self.mutes[0].permissions.model_dump().items()
            if name.startswith("can_send")
        ))
        await self.feed(self.admin, "/warn @member")
        self.assertIn("предупреждений: 4", self.sent[-1].text)

    async def test_non_admin_cannot_warn(self):
        await self.feed(text="/warn @admin")
        self.assertIn("только администраторам", self.sent[-1].text)
        self.assertEqual(await self.storage.increment_admin_warning(-100, 1), 1)
        self.assertFalse(self.mutes)

    async def test_reply_warn_supports_user_without_username_and_html_name(self):
        user = self.user.model_copy(update={"username": None, "first_name": "<Member>"})
        self.members[42] = ChatMemberMember(user=user)
        await self.feed(self.admin, "/warn", reply_to_message=self.message(user, "hello"))
        self.assertIn('href="tg://user?id=42"', self.sent[-1].text)
        self.assertIn("&lt;Member&gt;", self.sent[-1].text)

    async def test_unknown_username_gets_reply_guidance(self):
        await self.feed(self.admin, "/warn @unknown")
        self.assertIn("ответом", self.sent[-1].text)
        self.assertFalse(self.mutes)

    async def test_stale_username_does_not_warn_previous_owner(self):
        await self.feed(text="hello")
        renamed = self.user.model_copy(update={"username": "renamed"})
        self.members[42] = ChatMemberMember(user=renamed)
        await self.feed(self.admin, "/warn @member")
        self.assertIn("изменилось", self.sent[-1].text)
        self.assertIsNone(await self.storage.find_user_id(-100, "member"))
        self.assertEqual(await self.storage.increment_admin_warning(-100, 42), 1)

    async def test_admin_target_is_protected(self):
        await self.feed(self.admin, "/warn", reply_to_message=self.message(self.admin, "hi"))
        self.assertIn("Нельзя", self.sent[-1].text)
        self.assertFalse(self.mutes)

    async def test_anonymous_group_admin_can_warn_but_linked_channel_cannot(self):
        await self.feed(text="hello")
        await self.feed(text="/warn @member", sender_chat=Chat(id=-200, type="channel"))
        self.assertIn("только администраторам", self.sent[-1].text)
        await self.feed(text="/warn @member", sender_chat=self.chat)
        self.assertIn("предупреждений: 1", self.sent[-1].text)

    async def test_mute_error_preserves_warning_and_reports_failure(self):
        await self.feed(text="hello")
        await self.storage.increment_admin_warning(-100, 42)
        await self.storage.increment_admin_warning(-100, 42)
        self.deny_mute = True
        with self.assertLogs("app.handlers", level="ERROR"):
            await self.feed(self.admin, "/warn @member")
        self.assertIn("предупреждений: 3", self.sent[-1].text)
        self.assertIn("Не удалось выдать мут", self.sent[-1].text)

    async def test_concurrent_warnings_are_counted_once_each(self):
        await self.feed(text="hello")
        await asyncio.gather(*(self.feed(self.admin, "/warn @member") for _ in range(3)))
        self.assertEqual([m.text.rsplit(": ", 1)[1] for m in self.sent], ["1", "2", "3"])
        self.assertEqual(len(self.mutes), 1)

    async def test_all_games_delete_after_a_minute_and_maximum_mutes(self):
        for emoji, maximum in [("🎰", 64), ("🎲", 6), ("🎯", 6), ("🎳", 6), ("🏀", 5), ("⚽", 5)]:
            with self.subTest(emoji=emoji):
                self.members[42] = ChatMemberMember(user=self.user)
                self.mutes.clear()
                message = await self.feed(dice=dict(emoji=emoji, value=maximum))
                self.assertNotIn(
                    (-100, message.message_id),
                    await self.storage.due_deletions(message.date.timestamp() + 59),
                )
                self.assertIn(
                    (-100, message.message_id),
                    await self.storage.due_deletions(message.date.timestamp() + 60),
                )
                self.assertEqual(len(self.mutes), 1)
                self.assertAlmostEqual(self.mutes[0].until_date.timestamp(), time.time() + 300, delta=3)

    async def test_losing_and_forwarded_games_are_only_deleted(self):
        for emoji, maximum in [("🎰", 64), ("🎲", 6), ("🎯", 6), ("🎳", 6), ("🏀", 5), ("⚽", 5)]:
            await self.feed(dice=dict(emoji=emoji, value=maximum - 1))
        await self.feed(
            dice=dict(emoji="🎰", value=64),
            forward_origin=dict(type="user", date=int(time.time()), sender_user=self.user),
        )
        self.assertFalse(self.mutes)
        self.assertEqual(len(await self.storage.due_deletions(time.time() + 61)), 7)

    async def test_admin_jackpot_is_deleted_without_attempting_to_mute(self):
        await self.feed(self.admin, dice=dict(emoji="🎲", value=6))
        self.assertFalse(self.mutes)
        self.assertEqual(len(await self.storage.due_deletions(time.time() + 61)), 1)

    async def test_jackpot_cannot_shorten_day_mute(self):
        await self.feed(text="hello")
        for _ in range(3):
            await self.feed(self.admin, "/warn @member")
        await self.feed(dice=dict(emoji="🎲", value=6))
        self.assertEqual(len(self.mutes), 1)

    async def test_points_policy_keeps_its_own_counter_and_all_replies_expire(self):
        await self.feed(text="hello")
        await self.feed(self.admin, "/warn @member")
        for _ in range(3):
            await self.feed(text="баллы")
        self.assertEqual([m.text for m in self.sent[1:]], ["one", "two", "three"])
        self.assertEqual(len(self.mutes), 1)
        self.assertAlmostEqual(self.mutes[0].until_date.timestamp(), time.time() + 600, delta=3)
        self.assertEqual(await self.storage.increment_warning(-100, 42), 1)
        self.assertEqual(await self.storage.increment_admin_warning(-100, 42), 2)
        self.assertEqual(len(await self.storage.due_deletions(time.time() + 61)), 4)

    async def test_command_suffix_and_malformed_command(self):
        await self.feed(text="hello")
        await self.feed(self.admin, "/warn@kb_test_bot @member")
        self.assertIn("предупреждений: 1", self.sent[-1].text)
        await self.feed(self.admin, "/warn @member @admin")
        self.assertIn("Используйте", self.sent[-1].text)

    async def test_private_dice_is_ignored(self):
        await self.feed(chat=Chat(id=42, type="private"), dice=dict(emoji="🎲", value=6))
        self.assertFalse(self.mutes)
        self.assertEqual(await self.storage.due_deletions(time.time() + 61), [])

    async def test_new_member_still_receives_captcha_and_is_remembered(self):
        message = await self.feed(self.admin, new_chat_members=[self.user])
        self.captcha.start.assert_awaited_once()
        self.assertEqual(self.captcha.start.await_args.args[0], -100)
        self.assertEqual(self.captcha.start.await_args.args[1].id, self.user.id)
        self.assertEqual(await self.storage.find_user_id(-100, "member"), 42)
        self.assertTrue(any(
            isinstance(call.args[1], DeleteMessage)
            and call.args[1].message_id == message.message_id
            for call in self.bot.session.make_request.await_args_list
        ))

    async def test_plain_emoji_text_is_not_a_game(self):
        await self.feed(text="🎰 🎲 🎯 🎳 🏀 ⚽")
        self.assertFalse(self.mutes)
        self.assertEqual(await self.storage.due_deletions(time.time() + 61), [])

    async def test_points_policy_cannot_shorten_a_day_mute(self):
        await self.feed(text="hello")
        for _ in range(3):
            await self.feed(self.admin, "/warn @member")
        for _ in range(3):
            await self.feed(text="баллы")
        self.assertEqual(len(self.mutes), 1)

    async def test_left_user_is_not_warned(self):
        await self.feed(text="hello")
        self.members[42] = SimpleNamespace(status="left", user=self.user)
        await self.feed(self.admin, "/warn @member")
        self.assertIn("не состоит", self.sent[-1].text)
        self.assertEqual(await self.storage.increment_admin_warning(-100, 42), 1)

    async def test_sticker_pack_filter_still_deletes_only_forbidden_packs(self):
        for pack, expected_deletions in [("BLOCKED", 1), ("allowed", 0)]:
            self.bot.session.make_request.reset_mock()
            await self.feed(sticker=dict(
                file_id="file", file_unique_id="unique", type="regular",
                width=100, height=100, is_animated=False, is_video=False, set_name=pack,
            ))
            deletions = [
                call.args[1] for call in self.bot.session.make_request.await_args_list
                if isinstance(call.args[1], DeleteMessage)
            ]
            self.assertEqual(len(deletions), expected_deletions)
        self.assertEqual(await self.storage.due_deletions(time.time() + 61), [])
