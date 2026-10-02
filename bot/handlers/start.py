from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from apps.accounts.models import TelegramUser

from .. import services
from ..keyboards import main_menu
from ..render import esc

router = Router()

HELP = (
    "📬 <b>Почтовый бот</b>\n\n"
    "• Уведомления о новых письмах: от кого, кому, копия, тема, тип (новое / ответ / переслано), "
    "вложения и вся цепочка переписки.\n"
    "• Под каждым письмом: <b>Ответить</b>, <b>Ответить всем</b>, <b>Переслать</b>, <b>История</b>, "
    "<b>Полностью</b>, <b>Вложения</b>.\n"
    "• Быстрый ответ: просто ответьте (reply) на карточку письма в Telegram — бот подготовит ответ.\n"
    "• <b>Написать</b> — новое письмо с копией и файлами.\n\n"
    "Команды: /inbox /new /search /mailbox /settings /cancel"
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, user: TelegramUser):
    await state.clear()
    account = await services.get_account(user)
    text = f"👋 Здравствуйте, <b>{esc(user.full_name or user.username)}</b>!\n\n{HELP}"
    await message.answer(text, reply_markup=main_menu(user.is_superadmin))
    if not account:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Подключить почту", callback_data="acc:connect")
        await message.answer("📭 Почтовый ящик ещё не подключён.", reply_markup=kb.as_markup())


@router.message(Command("help"))
async def cmd_help(message: Message, user: TelegramUser):
    await message.answer(HELP, reply_markup=main_menu(user.is_superadmin))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, user: TelegramUser):
    await state.clear()
    await message.answer("❎ Действие отменено.", reply_markup=main_menu(user.is_superadmin))


@router.callback_query(F.data == "d:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("❎ Отменено.")
    await callback.answer()
