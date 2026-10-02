from aiogram import Router
from aiogram.types import CallbackQuery, Message

from apps.accounts.models import TelegramUser

from ..keyboards import main_menu

router = Router()


@router.message()
async def unknown_message(message: Message, user: TelegramUser):
    await message.answer("🤔 Не понял. Воспользуйтесь меню ниже или /help.", reply_markup=main_menu(user.is_superadmin))


@router.callback_query()
async def unknown_callback(callback: CallbackQuery):
    await callback.answer("Кнопка устарела", show_alert=False)
