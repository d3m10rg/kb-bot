from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    User,
)

from app.config import Settings
from app.cleanup import DELETE_AFTER_SECONDS
from app.domain import make_math_problem
from app.storage import Challenge, Storage

logger = logging.getLogger(__name__)

NO_SEND_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

FALLBACK_MEMBER_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


class CaptchaManager:
    def __init__(self, bot: Bot, storage: Storage, settings: Settings) -> None:
        self._bot = bot
        self._storage = storage
        self._settings = settings
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def restore_pending(self) -> None:
        for challenge in await self._storage.list_challenges():
            self._schedule_timeout(challenge)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def start(self, chat_id: int, user: User) -> None:
        if user.is_bot:
            return

        problem = make_math_problem()
        challenge = Challenge(
            token=secrets.token_urlsafe(6),
            chat_id=chat_id,
            user_id=user.id,
            answer=problem.answer,
            # The keyboard must remain available for the entire challenge.
            deadline=time.time() + min(
                self._settings.captcha_timeout_seconds, DELETE_AFTER_SECONDS
            ),
        )
        if not await self._storage.add_challenge(challenge):
            return  # Both Telegram join update types can report the same join.

        try:
            await self._bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user.id,
                permissions=NO_SEND_PERMISSIONS,
                use_independent_chat_permissions=True,
            )
            sent = await self._bot.send_message(
                chat_id=chat_id,
                text=(
                    f"Привет, {_mention(user)}! Чтобы попасть в чат, реши "
                    f"математическую задачку: {problem.left} + {problem.right} = ?"
                ),
                reply_markup=_answer_keyboard(challenge.token),
            )
            await self._storage.set_challenge_message(challenge.token, sent.message_id)
            challenge = Challenge(
                token=challenge.token,
                chat_id=challenge.chat_id,
                user_id=challenge.user_id,
                answer=challenge.answer,
                deadline=challenge.deadline,
                message_id=sent.message_id,
            )
            self._schedule_timeout(challenge)
        except Exception:
            await self._storage.claim_challenge(challenge.token)
            await self._restore_permissions_safely(chat_id, user.id)
            raise

    async def resolve(self, token: str, user_id: int, selected: int) -> str:
        challenge = await self._storage.get_challenge(token)
        if challenge is None:
            return "expired"
        if challenge.user_id != user_id:
            return "foreign"

        challenge = await self._storage.claim_challenge(token)
        if challenge is None:
            return "expired"
        self._cancel_timeout(token)

        if time.time() >= challenge.deadline:
            await self._kick(challenge)
            return "expired"
        if selected != challenge.answer:
            await self._kick(challenge)
            return "wrong"

        await self._restore_permissions(challenge.chat_id, challenge.user_id)
        await self._edit_challenge_message(
            challenge, "✅ Ответ правильный. Добро пожаловать в чат!"
        )
        return "correct"

    async def forget_user(self, chat_id: int, user_id: int) -> None:
        challenge = await self._storage.remove_user_challenge(chat_id, user_id)
        if challenge:
            self._cancel_timeout(challenge.token)

    def _schedule_timeout(self, challenge: Challenge) -> None:
        if challenge.token in self._tasks:
            return
        task = asyncio.create_task(
            self._timeout(challenge.token, challenge.deadline),
            name=f"captcha-timeout-{challenge.token}",
        )
        self._tasks[challenge.token] = task
        task.add_done_callback(lambda _: self._tasks.pop(challenge.token, None))

    def _cancel_timeout(self, token: str) -> None:
        task = self._tasks.pop(token, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    async def _timeout(self, token: str, deadline: float) -> None:
        await asyncio.sleep(max(0, deadline - time.time()))
        challenge = await self._storage.claim_challenge(token)
        if challenge is not None:
            await self._kick(challenge)

    async def _kick(self, challenge: Challenge) -> None:
        try:
            await self._bot.ban_chat_member(
                chat_id=challenge.chat_id, user_id=challenge.user_id
            )
            await self._bot.unban_chat_member(
                chat_id=challenge.chat_id,
                user_id=challenge.user_id,
                only_if_banned=True,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.exception(
                "Не удалось удалить пользователя %s из чата %s",
                challenge.user_id,
                challenge.chat_id,
            )
        await self._edit_challenge_message(
            challenge, "❌ Проверка не пройдена. Пользователь удалён из чата."
        )

    async def _restore_permissions(self, chat_id: int, user_id: int) -> None:
        chat = await self._bot.get_chat(chat_id)
        permissions = chat.permissions or FALLBACK_MEMBER_PERMISSIONS
        await self._bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=permissions,
            use_independent_chat_permissions=True,
        )

    async def _restore_permissions_safely(self, chat_id: int, user_id: int) -> None:
        try:
            await self._restore_permissions(chat_id, user_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.exception(
                "Не удалось вернуть права пользователю %s в чате %s",
                user_id,
                chat_id,
            )

    async def _edit_challenge_message(
        self, challenge: Challenge, text: str
    ) -> None:
        if challenge.message_id is None:
            return
        try:
            await self._bot.edit_message_text(
                chat_id=challenge.chat_id,
                message_id=challenge.message_id,
                text=text,
                reply_markup=None,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.info("Сообщение капчи уже удалено или недоступно")


def mute_until(minutes: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


async def mute_member(bot: Bot, chat_id: int, user_id: int, minutes: int) -> None:
    member = await bot.get_chat_member(chat_id, user_id)
    until_date = mute_until(minutes)
    if member.status == ChatMemberStatus.RESTRICTED and not member.can_send_messages:
        current_until = member.until_date
        # A jackpot or the points policy must not shorten an existing day-long mute.
        if current_until.timestamp() == 0 or current_until >= until_date:
            return
    await bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=NO_SEND_PERMISSIONS,
        use_independent_chat_permissions=True,
        until_date=until_date,
    )


def _answer_keyboard(token: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=str(number), callback_data=f"captcha:{token}:{number}")
        for number in range(1, 11)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons[0:5], buttons[5:10]]
    )


def _mention(user: User) -> str:
    if user.username:
        return f"@{html.escape(user.username)}"
    display_name = html.escape(user.full_name)
    return f'<a href="tg://user?id={user.id}">{display_name}</a>'

