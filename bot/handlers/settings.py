from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from apps.accounts.models import TelegramUser

from .. import services
from ..keyboards import BTN_SETTINGS, MENU_BUTTONS, cancel_kb
from ..render import esc
from ..states import SettingsSG

router = Router()


def _settings_view(user: TelegramUser, display_name: str | None):
    on = lambda v: "✅" if v else "❌"  # noqa: E731
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"{on(user.notifications_enabled)} Уведомления о новых письмах\n"
        f"{on(user.send_attachments)} Сразу присылать вложения вместе с уведомлением\n"
        f"✍️ Подпись: {esc(user.signature) or '<i>нет</i>'}\n"
        f"👤 Имя отправителя: {esc(display_name) or '<i>не задано</i>'}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{on(user.notifications_enabled)} Уведомления", callback_data="set:notif")
    kb.button(text=f"{on(user.send_attachments)} Вложения сразу", callback_data="set:att")
    kb.button(text="✍️ Подпись", callback_data="set:sig")
    kb.button(text="👤 Имя отправителя", callback_data="set:name")
    kb.adjust(2, 2)
    return text, kb.as_markup()


async def _show(message: Message, user: TelegramUser, edit: bool = False):
    account = await services.get_account(user)
    text, markup = _settings_view(user, account.display_name if account else None)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.message(F.text == BTN_SETTINGS)
@router.message(Command("settings"))
async def settings_menu(message: Message, state: FSMContext, user: TelegramUser):
    await state.clear()
    await _show(message, user)


@router.callback_query(F.data.in_({"set:notif", "set:att"}))
async def toggle(callback: CallbackQuery, user: TelegramUser):
    field = "notifications_enabled" if callback.data == "set:notif" else "send_attachments"
    user = await services.update_user(user.pk, **{field: not getattr(user, field)})
    await callback.answer("Сохранено")
    await _show(callback.message, user, edit=True)


@router.callback_query(F.data == "set:sig")
async def ask_signature(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsSG.signature)
    await callback.answer()
    await callback.message.answer("✍️ Отправьте подпись (будет добавляться в конец писем).\n"
                                  "Отправьте «-», чтобы убрать подпись.", reply_markup=cancel_kb())


@router.message(SettingsSG.signature, F.text, ~F.text.in_(MENU_BUTTONS))
async def got_signature(message: Message, state: FSMContext, user: TelegramUser):
    signature = "" if message.text.strip() == "-" else message.text.strip()[:1000]
    user = await services.update_user(user.pk, signature=signature)
    await state.clear()
    await _show(message, user)


@router.callback_query(F.data == "set:name")
async def ask_name(callback: CallbackQuery, state: FSMContext, user: TelegramUser):
    if not await services.get_account(user):
        return await callback.answer("Сначала подключите почту", show_alert=True)
    await state.set_state(SettingsSG.display_name)
    await callback.answer()
    await callback.message.answer("👤 Как подписывать отправителя? Например: <code>Иванов Иван</code>",
                                  reply_markup=cancel_kb())


@router.message(SettingsSG.display_name, F.text, ~F.text.in_(MENU_BUTTONS))
async def got_name(message: Message, state: FSMContext, user: TelegramUser):
    account = await services.get_account(user)
    if account:
        await services.update_account(account.pk, display_name=message.text.strip()[:255])
    await state.clear()
    await _show(message, user)
