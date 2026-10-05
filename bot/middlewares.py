import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, InlineQuery, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

from apps.accounts.models import TelegramUser

from . import services
from .render import esc

logger = logging.getLogger(__name__)


async def notify_superadmin_new_user(bot: Bot, user: TelegramUser) -> None:
    from django.conf import settings
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"adm:set:{user.pk}:active")
    kb.button(text="⛔ Отклонить", callback_data=f"adm:set:{user.pk}:blocked")
    username = f"@{esc(user.username)}" if user.username else "—"
    try:
        await bot.send_message(
            settings.SUPERADMIN_TELEGRAM_ID,
            f"🆕 <b>Запрос доступа</b>\n👤 {esc(user.full_name)}\n🔗 {username}\n🆔 <code>{user.telegram_id}</code>",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        logger.warning("Can't notify superadmin: %s", e)


class AuthMiddleware(BaseMiddleware):
    """Регистрирует пользователя, кладёт его в data['user'] и пускает только подтверждённых."""

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return None
        user, created = await services.register_user(tg_user.id, tg_user.username or "", tg_user.full_name or "")
        data["user"] = user
        if user.has_access:
            return await handler(event, data)

        if created:
            await notify_superadmin_new_user(data["bot"], user)
        if user.status == TelegramUser.Status.BLOCKED:
            text = "⛔ Доступ к боту закрыт."
        else:
            text = "⏳ Заявка на доступ отправлена администратору. Как только её подтвердят — придёт уведомление."
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, InlineQuery):
            await event.answer([], cache_time=60, is_personal=True)  # справочник — только своим
        return None
