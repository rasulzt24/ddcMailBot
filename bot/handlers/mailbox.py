"""Подключение / управление почтовым ящиком."""
import asyncio
import logging
import ssl

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from django.conf import settings

from apps.accounts.models import TelegramUser
from apps.mail.services import imap, smtp
from apps.mail.services.compose import EMAIL_RE

from .. import services
from ..keyboards import BTN_MAILBOX, MENU_BUTTONS, cancel_kb, main_menu
from ..render import esc, fmt_date
from ..states import ConnectSG

router = Router()
logger = logging.getLogger(__name__)

MAX_PASSWORD_ATTEMPTS = 3


async def show_mailbox(message: Message, user: TelegramUser):
    account = await services.get_account(user)
    kb = InlineKeyboardBuilder()
    if not account:
        kb.button(text="➕ Подключить почту", callback_data="acc:connect")
        await message.answer("📭 Почтовый ящик не подключён.", reply_markup=kb.as_markup())
        return
    stats = await services.account_stats(account)
    if account.needs_reauth:
        status = "🔐 пароль не подходит — смените пароль"
    elif not account.is_active:
        status = "⏸ на паузе"
    elif account.error_count:
        status = f"⚠️ ошибки ({account.error_count} подряд)"
    else:
        status = "✅ работает"
    text = (
        f"📬 <b>{esc(account.email)}</b>\n"
        f"Имя отправителя: {esc(account.display_name) or '—'}\n"
        f"Статус: {status}\n"
        f"Последняя проверка: {fmt_date(account.last_sync_at)}\n"
        f"Писем: 📥 {stats['incoming']} (непрочитанных {stats['unread']}) · 📤 {stats['outgoing']}\n"
        f"Контактов в адресной книге: {stats['contacts']}"
    )
    if account.last_error:
        text += f"\n\n<i>Последняя ошибка:</i> <code>{esc(account.last_error[:300])}</code>"
    kb.button(text="🔃 Проверить сейчас", callback_data="acc:sync")
    kb.button(text="⏸ Пауза" if account.is_active else "▶️ Включить", callback_data="acc:toggle")
    kb.button(text="🔑 Сменить пароль", callback_data="acc:repass")
    kb.button(text="🗑 Отключить", callback_data="acc:delete")
    kb.adjust(2, 2)
    await message.answer(text, reply_markup=kb.as_markup())


@router.message(F.text == BTN_MAILBOX)
@router.message(Command("mailbox"))
async def menu_mailbox(message: Message, state: FSMContext, user: TelegramUser):
    await state.clear()
    await show_mailbox(message, user)


@router.message(Command("connect"))
async def cmd_connect(message: Message, state: FSMContext):
    await _ask_email(message, state)


@router.callback_query(F.data == "acc:connect")
async def cb_connect(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _ask_email(callback.message, state)


@router.message(Command("password"))
async def cmd_repass(message: Message, state: FSMContext, user: TelegramUser):
    await _ask_new_password(message, state, user)


@router.callback_query(F.data == "acc:repass")
async def cb_repass(callback: CallbackQuery, state: FSMContext, user: TelegramUser):
    await callback.answer()
    await _ask_new_password(callback.message, state, user)


async def _ask_new_password(message: Message, state: FSMContext, user: TelegramUser):
    account = await services.get_account(user)
    if not account:
        return await _ask_email(message, state)
    await state.clear()
    await state.update_data(email=account.email, attempts=0)
    await state.set_state(ConnectSG.password)
    await message.answer(
        f"🔑 Отправьте <b>новый пароль</b> от почты <b>{esc(account.email)}</b>.\n"
        "<i>Сообщение с паролем будет сразу удалено, пароль хранится в зашифрованном виде.</i>",
        reply_markup=cancel_kb(),
    )


async def _ask_email(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ConnectSG.email)
    await message.answer("📧 Введите адрес вашей почты (например, <code>ivan.ivanov@company.kz</code>):",
                         reply_markup=cancel_kb())


@router.message(ConnectSG.email, F.text, ~F.text.in_(MENU_BUTTONS))
async def got_email(message: Message, state: FSMContext):
    email = message.text.strip().lower()
    if not EMAIL_RE.match(email):
        await message.answer("⚠️ Это не похоже на e-mail. Попробуйте ещё раз.", reply_markup=cancel_kb())
        return
    await state.update_data(email=email, attempts=0)
    await state.set_state(ConnectSG.password)
    await message.answer(
        "🔑 Отправьте пароль от почты.\n"
        "<i>Сообщение с паролем будет сразу удалено, пароль хранится в зашифрованном виде.</i>",
        reply_markup=cancel_kb(),
    )


def _try_login(email: str, password: str, style: str | None) -> tuple[str | None, str]:
    """Пробуем варианты логина: полный адрес и часть до @ (как в Exchange/AD).

    Если для домена уже известен рабочий формат — пробуем только его,
    чтобы лишний раз не копить неудачные попытки входа в AD.
    """
    local = email.split("@")[0]
    candidates = {"email": [email], "local": [local]}.get(style, [email, local])
    last_error = ""
    for login in dict.fromkeys(candidates):
        try:
            imap.check_login(settings.MAIL_IMAP_HOST, settings.MAIL_IMAP_PORT, login, password)
            return login, ""
        except imap.ImapAuthError as e:
            last_error = f"неверный логин или пароль ({e})"
        except ssl.SSLCertVerificationError as e:
            return None, f"сертификат сервера не доверенный ({e.verify_message}) — сообщите администратору"
        except Exception as e:
            return None, f"сервер недоступен: {e}"
    return None, last_error


def _check_smtp(login: str, password: str) -> str:
    try:
        smtp.check_login(settings.MAIL_SMTP_HOST, settings.MAIL_SMTP_PORT, settings.MAIL_SMTP_SECURITY,
                         login, password)
        return ""
    except Exception as e:
        return str(e)


@router.message(ConnectSG.password, F.text, ~F.text.in_(MENU_BUTTONS))
async def got_password(message: Message, state: FSMContext, user: TelegramUser):
    password = message.text
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    email = data["email"]
    wait = await message.answer("⏳ Проверяю доступ к почте…")

    style = await services.login_style(email.split("@")[-1])
    login, error = await asyncio.to_thread(_try_login, email, password, style)
    if not login:
        attempts = data.get("attempts", 0) + 1
        if attempts >= MAX_PASSWORD_ATTEMPTS:
            await state.clear()
            await wait.edit_text(f"❌ Не удалось войти: {esc(error)}\n\n"
                                 f"Попытки исчерпаны, чтобы не заблокировать учётную запись. "
                                 f"Проверьте пароль и попробуйте позже.")
            return
        await state.update_data(attempts=attempts)
        await wait.edit_text(f"❌ Не удалось войти: {esc(error)}\n\nОтправьте пароль ещё раз "
                             f"(осталось попыток: {MAX_PASSWORD_ATTEMPTS - attempts}) или /cancel.")
        return

    smtp_error = await asyncio.to_thread(_check_smtp, login, password)
    existing = await services.get_account(user)
    password_change = bool(existing and existing.email == email)
    await services.save_account(user, email, login, password)
    await state.clear()
    if password_change:
        text = (f"✅ Пароль для <b>{esc(email)}</b> обновлён — проверка почты и отправка писем снова работают.\n"
                f"Письма, пришедшие за время паузы, сейчас подтянутся.")
    else:
        text = (f"✅ Почта <b>{esc(email)}</b> подключена!\n\n"
                f"Загружаю последние {settings.MAIL_INITIAL_IMPORT} писем «Входящих» и «Отправленных» "
                f"для истории переписки — уведомления будут приходить о новых письмах.")
    if smtp_error:
        text += (f"\n\n⚠️ Входящие работают, но проверка отправки (SMTP) не прошла:\n"
                 f"<code>{esc(smtp_error[:300])}</code>\nСообщите администратору.")
    await wait.edit_text(text)
    # Сразу даём меню: кнопки снизу + команды в меню слева (кнопка «/» у поля ввода)
    await message.answer(
        "🏠 <b>Главное меню</b>\n\n"
        "Кнопки внизу экрана:\n"
        "📥 <b>Входящие</b> · ✉️ <b>Написать</b> · 📤 <b>Отправленные</b> · 🔎 <b>Поиск</b> · "
        "📬 <b>Моя почта</b> · ⚙️ <b>Настройки</b>\n\n"
        "Те же действия — в меню команд слева от поля ввода (/inbox, /new, /search …).\n"
        "Под каждым письмом — кнопки «Ответить», «Переслать», «История» и др.",
        reply_markup=main_menu(user.is_superadmin),
    )


@router.callback_query(F.data == "acc:sync")
async def cb_sync(callback: CallbackQuery, user: TelegramUser):
    account = await services.get_account(user)
    if account:
        await services.update_account(account.pk, last_sync_at=None, is_active=True)
    await callback.answer("🔃 Проверка запущена")


@router.callback_query(F.data == "acc:toggle")
async def cb_toggle(callback: CallbackQuery, user: TelegramUser):
    account = await services.get_account(user)
    if not account:
        return await callback.answer()
    if account.needs_reauth:
        return await callback.answer("Сначала смените пароль", show_alert=True)
    await services.update_account(account.pk, is_active=not account.is_active)
    await callback.answer("⏸ Синхронизация на паузе" if account.is_active else "▶️ Синхронизация включена")
    await callback.message.delete()
    await show_mailbox(callback.message, user)


@router.callback_query(F.data == "acc:delete")
async def cb_delete(callback: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, отключить и удалить историю", callback_data="acc:delete_yes")
    kb.button(text="↩️ Нет", callback_data="d:cancel")
    kb.adjust(1)
    await callback.message.answer("Отключить почту? Все сохранённые письма и вложения в боте будут удалены "
                                  "(в самом почтовом ящике ничего не изменится).", reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "acc:delete_yes")
async def cb_delete_yes(callback: CallbackQuery, user: TelegramUser):
    account = await services.get_account(user)
    if account:
        await services.delete_account(account.pk)
    await callback.message.edit_text("🗑 Почта отключена.")
    await callback.answer()
