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
from aiogram.types import Chat, ChatMemberAdministrator, ChatMemberMember, ChatMemberOwner, ChatMemberUpdated, Message, Update, User

from app.cleanup import MessageCleanup
from app.handlers import register_handlers
from app.storage import Challenge, Storage
from tests.test_moderation import make_restricted, make_settings


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
            123: ChatMemberAdministrator(
                user=self.bot_user, is_anonymous=False,
                **{name: name == "can_restrict_members" for name in ChatMemberAdministrator.model_fields if name.startswith("can_")},
            ),
        }
        self.sent = []
        self.mutes = []
        self.deny_mute = False
        self.ignore_mute = False
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
            if not self.ignore_mute:
                user = self.members[method.user_id].user
                self.members[method.user_id] = (
                    ChatMemberMember(user=user) if method.permissions.can_send_messages
                    else make_restricted(user, method.until_date)
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

    async def prepare_unban(self):
        await self.storage.remember_user(-100, 42, "member")
        await self.storage.increment_admin_warning(-100, 42)
        self.members[42] = make_restricted(self.user, int(time.time()) + 86400)
        await self.storage.set_mute(-100, 42, time.time() + 86400)

    async def test_unban_removes_mute_preserves_warnings_and_updates_stats(self):
        await self.prepare_unban()
        await self.feed(self.admin, "/unban @MeMbEr")
        self.assertEqual(self.sent[-1].text, "С пользователя @member снят мут.")
        self.assertIsNone(await self.storage.active_mute(-100, 42))
        self.assertTrue(all(self.mutes[-1].permissions.model_dump().values()))
        self.assertEqual((await self.storage.moderation_stats(-100))[0].warnings, 1)
        await self.feed(self.admin, text="/stats")
        self.assertIn("@member — предупреждений: 1", self.sent[-1].text)
        self.assertNotIn("— разбан:", self.sent[-1].text)

    async def test_unban_requires_admin(self):
        await self.prepare_unban()
        await self.feed(text="/unban @member")
        self.assertIn("только администраторам", self.sent[-1].text)
        self.assertFalse(self.mutes)
        self.assertIsNotNone(await self.storage.active_mute(-100, 42))

    async def test_unban_failure_keeps_saved_mute(self):
        await self.prepare_unban()
        self.deny_mute = True
        await self.feed(self.admin, "/unban @member")
        self.assertIn("Не удалось снять мут", self.sent[-1].text)
        self.assertIsNotNone(await self.storage.active_mute(-100, 42))

    async def test_unban_checks_telegram_confirmation(self):
        await self.prepare_unban()
        self.ignore_mute = True
        await self.feed(self.admin, "/unban @member")
        self.assertIn("не подтвердил", self.sent[-1].text)
        self.assertIsNotNone(await self.storage.active_mute(-100, 42))

    async def test_unban_stale_username_cannot_target_previous_owner(self):
        await self.prepare_unban()
        self.members[42] = make_restricted(
            self.user.model_copy(update={"username": "renamed"}), int(time.time()) + 86400
        )
        await self.feed(self.admin, "/unban @member")
        self.assertIn("изменилось", self.sent[-1].text)
        self.assertFalse(self.mutes)

    async def test_unban_reply_and_repeated_command(self):
        await self.prepare_unban()
        await self.feed(self.admin, "/unban", reply_to_message=self.message(text="hello"))
        self.assertEqual(self.sent[-1].text, "С пользователя @member снят мут.")
        await self.feed(self.admin, "/unban @member")
        self.assertIn("нет мута", self.sent[-1].text)
        self.assertEqual(len(self.mutes), 1)

    async def test_unban_unknown_username(self):
        await self.feed(self.admin, "/unban @unknown")
        self.assertIn("ответом", self.sent[-1].text)
        self.assertFalse(self.mutes)

    async def test_unban_does_not_bypass_pending_captcha(self):
        await self.prepare_unban()
        await self.storage.add_challenge(Challenge(
            token="pending", chat_id=-100, user_id=42, answer=2, deadline=time.time() + 60,
        ))
        await self.feed(self.admin, "/unban @member")
        self.assertIn("пройти капчу", self.sent[-1].text)
        self.assertFalse(self.mutes)
        self.assertIsNotNone(await self.storage.active_mute(-100, 42))

    async def test_three_admin_warnings_mute_for_a_day_and_keep_count(self):
        await self.feed(text="hello")
        for count in range(1, 4):
            await self.feed(self.admin, "/warn @MeMbEr")
            expected = (
                "Пользователю @member дано предупреждение.\n"
                f"Осталось предупреждений до бана: {3 - count}"
            )
            if count == 3:
                expected += "\n\n@member, ты видимо с 3 раза не понял, посиди в бане на сутки"
            self.assertEqual(self.sent[-1].text, expected)
            self.assertEqual(len(self.mutes), int(count == 3))
        self.assertAlmostEqual(self.mutes[0].until_date, time.time() + 86400, delta=3)
        self.assertTrue(self.mutes[0].use_independent_chat_permissions)
        self.assertTrue(all(
            value is False for name, value in self.mutes[0].permissions.model_dump().items()
            if name.startswith("can_send")
        ))
        await self.feed(self.admin, "/warn @member")
        self.assertIn("Осталось предупреждений до бана: 0", self.sent[-1].text)
        self.assertEqual((await self.storage.moderation_stats(-100))[0].warnings, 4)

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
        self.assertIn("Осталось предупреждений до бана: 2", self.sent[-1].text)

    async def test_mute_error_preserves_warning_and_reports_failure(self):
        await self.feed(text="hello")
        await self.storage.increment_admin_warning(-100, 42)
        await self.storage.increment_admin_warning(-100, 42)
        self.deny_mute = True
        with self.assertLogs("app.handlers", level="ERROR"):
            await self.feed(self.admin, "/warn @member")
        self.assertIn("Осталось предупреждений до бана: 0", self.sent[-1].text)
        self.assertIn("Не удалось выдать мут", self.sent[-1].text)
        self.assertNotIn("посиди в бане", self.sent[-1].text)

    async def test_concurrent_warnings_are_counted_once_each(self):
        await self.feed(text="hello")
        await asyncio.gather(*(self.feed(self.admin, "/warn @member") for _ in range(3)))
        self.assertEqual([m.text.split(": ", 1)[1].splitlines()[0] for m in self.sent], ["2", "1", "0"])
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
                self.assertAlmostEqual(self.mutes[0].until_date, time.time() + 300, delta=3)
                self.assertEqual(self.sent[-1].text, "Поздравляю с успешным депом, вот тебе мут на 5 минут")
                self.assertEqual(await self.storage.active_mute(-100, 42), self.mutes[0].until_date)

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
        with self.assertLogs("app.handlers", level="ERROR"):
            await self.feed(self.admin, dice=dict(emoji="🎲", value=6))
        self.assertFalse(self.mutes)
        self.assertIn("не позволяет", self.sent[-1].text)
        self.assertEqual(len(await self.storage.due_deletions(time.time() + 61)), 2)

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
        self.assertAlmostEqual(self.mutes[0].until_date, time.time() + 600, delta=3)
        self.assertEqual(await self.storage.increment_warning(-100, 42), 1)
        self.assertEqual(await self.storage.increment_admin_warning(-100, 42), 2)
        self.assertEqual(len(await self.storage.due_deletions(time.time() + 61)), 4)

    async def test_command_suffix_and_malformed_command(self):
        await self.feed(text="hello")
        await self.feed(self.admin, "/warn@kb_test_bot @member")
        self.assertIn("Осталось предупреждений до бана: 2", self.sent[-1].text)
        await self.feed(self.admin, "/warn @<member>")
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

    async def test_warn_with_reason_matches_requested_text(self):
        await self.feed(text="hello")
        await self.feed(self.admin, "/warn @member за мат в чате")
        self.assertEqual(self.sent[-1].text,
            "Пользователю @member дано предупреждение.\n"
            "Причина: за мат в чате\nОсталось предупреждений до бана: 2")

    async def test_reply_warn_reason_escapes_html(self):
        await self.feed(self.admin, "/warn за <b>мат</b> & флуд", reply_to_message=self.message(text="hello"))
        self.assertIn("Причина: за &lt;b&gt;мат&lt;/b&gt; &amp; флуд", self.sent[-1].text)

    async def test_long_reason_is_preserved_across_messages(self):
        import html
        reason = "<" * 3800
        await self.feed(text="hello")
        await self.feed(self.admin, "/warn @member " + reason)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(sum(html.unescape(m.text).count("<") for m in self.sent), len(reason))
        self.assertTrue(all(len(html.unescape(m.text)) <= 4096 for m in self.sent))

    async def test_partial_indefinite_restriction_does_not_skip_jackpot_mute(self):
        self.members[42] = make_restricted(self.user, 0, can_send_other_messages=True)
        await self.feed(dice=dict(emoji="🎲", value=6))
        self.assertEqual(len(self.mutes), 1)
        self.assertAlmostEqual(self.mutes[0].until_date, time.time() + 300, delta=3)
        self.assertFalse(self.members[42].can_send_other_messages)
        self.assertIn("Поздравляю с успешным депом", self.sent[-1].text)

    async def test_jackpot_api_failure_is_visible_without_false_congratulation(self):
        self.deny_mute = True
        with self.assertLogs("app.handlers", level="ERROR"):
            await self.feed(dice=dict(emoji="🎰", value=64))
        self.assertIn("Мут не выдан", self.sent[-1].text)
        self.assertIn("no rights", self.sent[-1].text)
        self.assertNotIn("вот тебе мут", self.sent[-1].text)
        self.assertIsNone(await self.storage.active_mute(-100, 42))

    async def test_jackpot_checks_bot_rights(self):
        self.members[123] = self.members[123].model_copy(update={"can_restrict_members": False})
        with self.assertLogs("app.handlers", level="ERROR"):
            await self.feed(dice=dict(emoji="🎯", value=6))
        self.assertIn("разрешением ограничивать участников", self.sent[-1].text)
        self.assertFalse(self.mutes)

    async def test_jackpot_verifies_actual_member_permissions(self):
        self.ignore_mute = True
        with self.assertLogs("app.handlers", level="ERROR"):
            await self.feed(dice=dict(emoji="🎳", value=6))
        self.assertIn("Telegram не подтвердил мут", self.sent[-1].text)
        self.assertIsNone(await self.storage.active_mute(-100, 42))

    async def test_group_jackpot_explains_supergroup_requirement(self):
        await self.feed(chat=Chat(id=-100, type="group"), dice=dict(emoji="🎲", value=6))
        self.assertIn("только в супергруппе", self.sent[-1].text)
        self.assertFalse(self.mutes)

    async def test_emoji_variation_selector_still_triggers_jackpot(self):
        await self.feed(dice=dict(emoji="⚽\ufe0f", value=5))
        self.assertEqual(len(self.mutes), 1)

    async def test_warning_during_captcha_sets_a_finite_day_mute(self):
        await self.storage.add_challenge(Challenge("captcha", -100, 42, 7, time.time() + 60))
        self.members[42] = make_restricted(self.user, 0)
        await self.storage.increment_admin_warning(-100, 42)
        await self.storage.increment_admin_warning(-100, 42)
        await self.feed(self.admin, "/warn", reply_to_message=self.message(text="hi"))
        self.assertEqual(len(self.mutes), 1)
        self.assertAlmostEqual(await self.storage.active_mute(-100, 42), time.time() + 86400, delta=3)

    async def test_stats_requires_admin_and_does_not_expose_data(self):
        await self.prepare_unban()
        await self.feed(text="/stats")
        self.assertEqual(self.sent[-1].text, "Команда /stats доступна только администраторам чата.")
        self.assertNotIn("@member", self.sent[-1].text)

    async def test_stats_trusts_only_anonymous_admin_of_this_group(self):
        await self.feed(text="/stats", sender_chat=Chat(id=-200, type="channel"))
        self.assertIn("только администраторам", self.sent[-1].text)
        await self.feed(text="/stats", sender_chat=self.chat)
        self.assertIn("Предупреждения (1–2)", self.sent[-1].text)

    async def test_unban_trusts_only_anonymous_admin_of_this_group(self):
        await self.prepare_unban()
        await self.feed(text="/unban @member", sender_chat=Chat(id=-200, type="channel"))
        self.assertFalse(self.mutes)
        await self.feed(text="/unban @member", sender_chat=self.chat)
        self.assertEqual(self.sent[-1].text, "С пользователя @member снят мут.")

    async def test_stats_has_warning_and_mute_sections_scoped_to_chat(self):
        now = int(time.time())
        for user_id, count, until in [(42, 1, None), (43, 2, None), (44, 3, now + 86400), (45, 0, now + 300)]:
            user = User(id=user_id, is_bot=False, first_name="User", username=f"member{user_id}")
            self.members[user_id] = ChatMemberMember(user=user) if until is None else make_restricted(user, until)
            await self.storage.remember_user(-100, user_id, user.username)
            for _ in range(count):
                await self.storage.increment_admin_warning(-100, user_id)
            if until:
                await self.storage.set_mute(-100, user_id, until)
        await self.storage.increment_admin_warning(-200, 99)
        await self.feed(self.admin, text="/stats")
        warned, muted = self.sent[-1].text.split("<b>Забаненные пользователи (мут):</b>")
        self.assertIn("@member42", warned)
        self.assertIn("@member43", warned)
        self.assertNotIn("@member44", warned)
        self.assertNotIn("@member45", warned)
        self.assertIn("@member44", muted)
        self.assertIn("@member45", muted)
        self.assertIn("МСК", muted)
        self.assertNotIn("99", self.sent[-1].text)
        self.assertIn((-100, self.sent[-1].message_id), await self.storage.due_deletions(time.time() + 61))

    async def test_stats_removes_expired_and_early_lifted_mutes(self):
        for until in [time.time() - 10, time.time() + 86400]:
            await self.storage.set_mute(-100, 42, until)
            await self.feed(self.admin, text="/stats")
            self.assertNotIn("разбан:", self.sent[-1].text)
            self.assertIsNone(await self.storage.active_mute(-100, 42))

    async def test_stats_discovers_existing_day_mute_from_old_warning_counter(self):
        for _ in range(3):
            await self.storage.increment_admin_warning(-100, 42)
        until = int(time.time()) + 86400
        self.members[42] = make_restricted(self.user, until)
        await self.feed(self.admin, text="/stats")
        self.assertIn("@member — разбан:", self.sent[-1].text)
        self.assertEqual(await self.storage.active_mute(-100, 42), until)

    async def test_stats_shows_indefinite_mute_but_not_captcha(self):
        await self.storage.increment_admin_warning(-100, 42)
        self.members[42] = make_restricted(self.user, 0)
        await self.feed(self.admin, text="/stats")
        self.assertIn("разбан: бессрочно", self.sent[-1].text)
        await self.storage.clear_mute(-100, 42)
        await self.storage.add_challenge(Challenge("pending", -100, 42, 7, time.time() + 60))
        await self.feed(self.admin, text="/stats")
        self.assertNotIn("разбан:", self.sent[-1].text)

    async def test_stats_splits_long_lists_without_dropping_users(self):
        for user_id in range(100, 180):
            user = User(id=user_id, is_bot=False, first_name="User", username=f"member{user_id}")
            self.members[user_id] = ChatMemberMember(user=user)
            await self.storage.increment_admin_warning(-100, user_id)
        await self.feed(self.admin, text="/stats")
        self.assertGreater(len(self.sent), 1)
        self.assertTrue(all(len(m.text) <= 3500 for m in self.sent))
        combined = "\n".join(m.text for m in self.sent)
        for user_id in range(100, 180):
            self.assertEqual(combined.count(f"@member{user_id} —"), 1)

    async def test_member_update_records_manual_mute_for_stats(self):
        until = int(time.time()) + 600
        self.members[42] = make_restricted(self.user, until)
        event = ChatMemberUpdated(
            chat=self.chat, from_user=self.admin, date=int(time.time()),
            old_chat_member=ChatMemberMember(user=self.user),
            new_chat_member=self.members[42],
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=100, chat_member=event))
        await self.feed(self.admin, text="/stats")
        self.assertIn("@member — разбан:", self.sent[-1].text)
        self.assertEqual(await self.storage.active_mute(-100, 42), until)
