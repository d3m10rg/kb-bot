from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import (
    ChatMemberUpdatedFilter, Command, CommandObject, JOIN_TRANSITION, LEAVE_TRANSITION,
)
from aiogram.types import CallbackQuery, ChatMemberUpdated, ChatPermissions, Message

from app.cleanup import MessageCleanup
from app.config import Settings
from app.domain import contains_points_word, is_jackpot
from app.moderation import CaptchaManager, ModerationError, _mention, mute_deadline, mute_member
from app.storage import Storage

logger = logging.getLogger(__name__)

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
PRIVILEGED_STATUSES = {
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.ADMINISTRATOR,
}


def register_handlers(
    captcha: CaptchaManager, storage: Storage, settings: Settings,
    cleanup: MessageCleanup,
) -> Router:
    local_router = Router(name="moderation")
    moderation_lock = storage.moderation_lock

    @local_router.message.outer_middleware()
    async def remember_sender(
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        message: Message,
        data: dict[str, Any],
    ) -> Any:
        if message.chat.type in GROUP_TYPES:
            users = list(message.new_chat_members or [])
            if message.from_user and not message.sender_chat:
                users.append(message.from_user)
            for user in users:
                if not user.is_bot:
                    await storage.remember_user(message.chat.id, user.id, user.username)
        return await handler(message, data)

    @local_router.chat_member.outer_middleware()
    async def remember_member(
        handler: Callable[[ChatMemberUpdated, dict[str, Any]], Awaitable[Any]],
        event: ChatMemberUpdated,
        data: dict[str, Any],
    ) -> Any:
        user = event.new_chat_member.user
        if event.chat.type in GROUP_TYPES and not user.is_bot:
            await storage.remember_user(event.chat.id, user.id, user.username)
            async with moderation_lock:
                if not await storage.has_challenge(event.chat.id, user.id):
                    try:
                        # Read current state; queued updates can describe an older restriction.
                        member = await data["bot"].get_chat_member(event.chat.id, user.id)
                        deadline = mute_deadline(member)
                        if deadline is None:
                            await storage.clear_mute(event.chat.id, user.id)
                        else:
                            await storage.set_mute(event.chat.id, user.id, deadline)
                    except TelegramAPIError:
                        logger.exception("Не удалось обновить состояние мута пользователя %s", user.id)
        return await handler(event, data)

    async def is_chat_admin(message: Message, bot: Bot) -> bool:
        if message.sender_chat:
            return message.sender_chat.id == message.chat.id
        if message.from_user and not message.from_user.is_bot:
            sender = await bot.get_chat_member(message.chat.id, message.from_user.id)
            return sender.status in PRIVILEGED_STATUSES
        return False

    @local_router.message(Command("warn"))
    async def warn(message: Message, bot: Bot, command: CommandObject) -> None:
        if message.chat.type not in GROUP_TYPES:
            return
        is_admin = await is_chat_admin(message, bot)
        if not is_admin:
            await message.answer("Команда /warn доступна только администраторам чата.")
            return
        if message.chat.type != ChatType.SUPERGROUP:
            await message.answer("Для предупреждений с мутом нужна супергруппа.")
            return

        username = None
        reason = None
        reply = message.reply_to_message
        args = (command.args or "").strip()
        if args.startswith("@"):
            parts = args.split(maxsplit=1)
            if not re.fullmatch(r"@[A-Za-z0-9_]{1,32}", parts[0]):
                await message.answer("Используйте /warn @username [причина] или /warn ответом на сообщение.")
                return
            username = parts[0][1:]
            reason = parts[1].strip() if len(parts) == 2 else None
            user_id = await storage.find_user_id(message.chat.id, username)
            if (
                reply and reply.from_user and not reply.sender_chat
                and (reply.from_user.username or "").casefold() == username.casefold()
            ):
                user_id = reply.from_user.id
        elif reply and reply.from_user and not reply.sender_chat:
            user_id = reply.from_user.id
            reason = args or None
        else:
            await message.answer("Используйте /warn @username [причина] или /warn ответом на сообщение.")
            return
        if user_id is None:
            await message.answer(
                "Пользователь пока неизвестен боту. Отправьте /warn ответом на его сообщение."
            )
            return

        async with moderation_lock:
            try:
                target = await bot.get_chat_member(message.chat.id, user_id)
            except (TelegramBadRequest, TelegramForbiddenError):
                await message.answer("Не удалось найти участника. Проверьте права бота в чате.")
                return
            user = target.user
            await storage.remember_user(message.chat.id, user.id, user.username)
            if username and (user.username or "").casefold() != username.casefold():
                await message.answer(
                    "Имя пользователя изменилось. Отправьте /warn ответом на его сообщение."
                )
                return
            if target.status in PRIVILEGED_STATUSES or user.is_bot:
                await message.answer("Нельзя выдать предупреждение администратору или боту.")
                return
            if target.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED} or (
                target.status == ChatMemberStatus.RESTRICTED and not target.is_member
            ):
                await message.answer("Пользователь уже не состоит в чате.")
                return

            count = await storage.increment_admin_warning(message.chat.id, user.id, reason)
            text = f"Пользователю {_mention(user)} дано предупреждение.\n"
            if reason:
                text += f"Причина: {html.escape(reason[:1500])}\n"
            text += f"Осталось предупреждений до бана: {max(0, 3 - count)}"
            if count >= 3:
                try:
                    await mute_member(bot, message.chat.id, user.id, 24 * 60, storage)
                except (ModerationError, TelegramAPIError) as error:
                    logger.exception("Не удалось выдать мут после /warn")
                    text += f"\nНе удалось выдать мут на сутки. {_restriction_error(error)}"
                else:
                    text += (
                        f"\n\n{_mention(user)}, ты видимо с 3 раза не понял, "
                        "посиди в бане на сутки"
                    )
            await message.answer(text)
            if reason:
                for offset in range(1500, len(reason), 1500):
                    await message.answer(
                        f"Причина (продолжение): {html.escape(reason[offset:offset + 1500])}"
                    )

    @local_router.message(Command("unban"))
    async def unban(message: Message, bot: Bot, command: CommandObject) -> None:
        if message.chat.type not in GROUP_TYPES:
            return
        is_admin = await is_chat_admin(message, bot)
        if not is_admin:
            await message.answer("Команда /unban доступна только администраторам чата.")
            return
        if message.chat.type != ChatType.SUPERGROUP:
            await message.answer("Для снятия мута нужна супергруппа.")
            return

        args = (command.args or "").strip()
        reply = message.reply_to_message
        username = None
        if re.fullmatch(r"@[A-Za-z0-9_]{1,32}", args):
            username = args[1:]
            user_id = await storage.find_user_id(message.chat.id, username)
            if (
                reply and reply.from_user and not reply.sender_chat
                and (reply.from_user.username or "").casefold() == username.casefold()
            ):
                user_id = reply.from_user.id
        elif not args and reply and reply.from_user and not reply.sender_chat:
            user_id = reply.from_user.id
        else:
            await message.answer("Используйте /unban @username или /unban ответом на сообщение.")
            return
        if user_id is None:
            await message.answer("Пользователь пока неизвестен боту. Отправьте /unban ответом на его сообщение.")
            return

        async with moderation_lock:
            try:
                target = await bot.get_chat_member(message.chat.id, user_id)
                user = target.user
                await storage.remember_user(message.chat.id, user.id, user.username)
                if username and (user.username or "").casefold() != username.casefold():
                    await message.answer("Имя пользователя изменилось. Отправьте /unban ответом на его сообщение.")
                    return
                if target.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED} or (
                    target.status == ChatMemberStatus.RESTRICTED and not target.is_member
                ):
                    await message.answer("Пользователь уже не состоит в чате.")
                    return
                if await storage.has_challenge(message.chat.id, user.id):
                    await message.answer("Сначала пользователь должен пройти капчу. Затем повторите /unban.")
                    return
                if target.status != ChatMemberStatus.RESTRICTED:
                    await storage.clear_mute(message.chat.id, user.id)
                    await message.answer(f"У пользователя {_mention(user)} нет мута.")
                    return
                # Telegram lifts individual restrictions when all permissions are True.
                result = await bot.restrict_chat_member(
                    chat_id=message.chat.id, user_id=user.id,
                    permissions=ChatPermissions(**{
                        name: True for name in ChatPermissions.model_fields
                        if name.startswith("can_")
                    }),
                    use_independent_chat_permissions=True,
                )
                confirmed = await bot.get_chat_member(message.chat.id, user.id)
                if not result or confirmed.status == ChatMemberStatus.RESTRICTED:
                    raise ModerationError("Telegram не подтвердил снятие ограничений.")
                await storage.clear_mute(message.chat.id, user.id)
            except (ModerationError, TelegramAPIError) as error:
                logger.exception("Не удалось снять мут после /unban")
                await message.answer(f"Не удалось снять мут. {_restriction_error(error)}")
                return
            await message.answer(f"С пользователя {_mention(user)} снят мут.")

    @local_router.message(Command("stats"))
    async def stats(message: Message, bot: Bot) -> None:
        if message.chat.type not in GROUP_TYPES:
            return
        if not await is_chat_admin(message, bot):
            await message.answer("Команда /stats доступна только администраторам чата.")
            return
        warnings = []
        muted = []
        stale = False
        for entry in await storage.moderation_stats(message.chat.id):
            label = (
                f"@{html.escape(entry.username)}" if entry.username
                else f'<a href="tg://user?id={entry.user_id}">ID {entry.user_id}</a>'
            )
            until_date = entry.muted_until
            eligible = True
            async with moderation_lock:
                try:
                    member = await bot.get_chat_member(message.chat.id, entry.user_id)
                    label = _mention(member.user)
                    await storage.remember_user(
                        message.chat.id, entry.user_id, member.user.username
                    )
                    eligible = member.status not in PRIVILEGED_STATUSES and not member.user.is_bot
                    until_date = mute_deadline(member) if eligible else None
                    # The temporary join captcha is not a moderation punishment.
                    if until_date == 0 and await storage.has_challenge(message.chat.id, entry.user_id):
                        until_date = await storage.active_mute(message.chat.id, entry.user_id)
                    if until_date is None:
                        await storage.clear_mute(message.chat.id, entry.user_id)
                    else:
                        await storage.set_mute(message.chat.id, entry.user_id, until_date)
                except TelegramAPIError:
                    logger.exception("Не удалось обновить /stats для пользователя %s", entry.user_id)
                    stale = True
            if eligible and 0 < entry.warnings < 3:
                warnings.append(
                    f"• {label} — предупреждений: {entry.warnings}; "
                    f"осталось до бана: {3 - entry.warnings}"
                )
            if eligible and until_date is not None and (until_date == 0 or until_date > time.time()):
                muted.append(f"• {label} — разбан: {_format_deadline(until_date)}")
        lines = [
            "<b>Предупреждения (1–2):</b>", *(warnings or ["Нет пользователей."]), "",
            "<b>Забаненные пользователи (мут):</b>", *(muted or ["Нет пользователей."]),
        ]
        if stale:
            lines += ["", "Не все ограничения удалось проверить в Telegram; для них показаны сохранённые данные."]
        await _answer_lines(message, lines)

    @local_router.message(F.dice)
    async def dice_policy(message: Message, bot: Bot) -> None:
        if message.chat.type not in GROUP_TYPES or message.dice is None:
            return
        await cleanup.schedule(message)
        if (
            message.from_user is None or message.from_user.is_bot
            or message.forward_origin is not None
            or not is_jackpot(message.dice.emoji, message.dice.value)
        ):
            return
        if message.chat.type != ChatType.SUPERGROUP:
            await message.answer("Выигрыш! Мут не выдан: Telegram разрешает мутить участников только в супергруппе.")
            return
        if message.sender_chat is not None:
            await message.answer("Выигрыш! Мут не выдан: сообщение отправлено от имени чата, а не пользователя.")
            return
        async with moderation_lock:
            try:
                until_date = await mute_member(bot, message.chat.id, message.from_user.id, 5, storage)
            except (ModerationError, TelegramAPIError) as error:
                logger.exception(
                    "Не удалось выдать мут за джекпот: chat=%s user=%s emoji=%s value=%s",
                    message.chat.id, message.from_user.id, message.dice.emoji, message.dice.value,
                )
                await message.answer(f"Выигрыш! Мут не выдан. {_restriction_error(error)}")
            else:
                text = "Поздравляю с успешным депом, вот тебе мут на 5 минут"
                if until_date == 0 or until_date > time.time() + 301:
                    text += "\nДействующий более долгий мут сохранён."
                await message.answer(text)

    @local_router.message(F.new_chat_members)
    async def new_members(message: Message) -> None:
        if message.chat.type not in GROUP_TYPES:
            return
        try:
            await message.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.exception("Не удалось удалить системное сообщение о входе")
        for user in message.new_chat_members or []:
            await captcha.start(message.chat.id, user)

    @local_router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
    async def member_joined(event: ChatMemberUpdated) -> None:
        # Fallback for join paths that do not produce a service message.
        await captcha.start(event.chat.id, event.new_chat_member.user)

    @local_router.chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
    async def member_left(event: ChatMemberUpdated) -> None:
        await captcha.forget_user(event.chat.id, event.new_chat_member.user.id)

    @local_router.callback_query(F.data.startswith("captcha:"))
    async def captcha_answer(callback: CallbackQuery) -> None:
        if callback.data is None:
            return
        try:
            _, token, raw_answer = callback.data.split(":", maxsplit=2)
            selected = int(raw_answer)
        except (ValueError, TypeError):
            await callback.answer("Некорректный ответ", show_alert=True)
            return

        result = await captcha.resolve(token, callback.from_user.id, selected)
        responses = {
            "correct": ("Верно!", False),
            "wrong": ("Неверно. Вы удалены из чата.", True),
            "foreign": ("Эта задача предназначена другому пользователю.", True),
            "expired": ("Эта задача уже недоступна.", True),
        }
        text, show_alert = responses[result]
        await callback.answer(text, show_alert=show_alert)

    @local_router.message(F.sticker)
    async def forbidden_sticker(message: Message) -> None:
        if message.chat.type not in GROUP_TYPES or message.sticker is None:
            return
        set_name = message.sticker.set_name
        if set_name and set_name.casefold() in settings.forbidden_sticker_packs:
            try:
                await message.delete()
            except (TelegramBadRequest, TelegramForbiddenError):
                logger.exception("Не удалось удалить запрещённый стикер")

    @local_router.message()
    async def points_policy(message: Message, bot: Bot) -> None:
        if (
            message.chat.type not in GROUP_TYPES
            or message.from_user is None
            or message.from_user.is_bot
            or not contains_points_word(message.text or message.caption)
        ):
            return

        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status in PRIVILEGED_STATUSES:
            return

        async with moderation_lock:
            attempt = await storage.increment_warning(
                message.chat.id, message.from_user.id
            )
            # If data was manually changed above 3, treat it as the third attempt.
            attempt = min(attempt, 3)
            await message.answer(settings.warning_texts[attempt - 1])

            if attempt == 3:
                await mute_member(
                    bot, message.chat.id, message.from_user.id, settings.mute_minutes, storage
                )
                await storage.reset_warnings(message.chat.id, message.from_user.id)

    return local_router


def _restriction_error(error: Exception) -> str:
    if isinstance(error, ModerationError):
        return html.escape(str(error))
    detail = html.escape(error.message[:300])
    return f"Проверьте права бота. Ответ Telegram: {detail}"


def _format_deadline(timestamp: float) -> str:
    if timestamp == 0:
        return "бессрочно"
    moscow = timezone(timedelta(hours=3))
    return datetime.fromtimestamp(timestamp, moscow).strftime("%d.%m.%Y %H:%M:%S МСК")


async def _answer_lines(message: Message, lines: list[str]) -> None:
    # Keep HTML tags intact while staying below Telegram's message length limit.
    chunk = ""
    for line in lines:
        if chunk and len(chunk) + len(line) + 1 > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += ("\n" if chunk else "") + line
    if chunk:
        await message.answer(chunk)
