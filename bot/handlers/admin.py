"""Панель суперадмина: пользователи, доступ, проблемные ящики, рассылка."""
import asyncio

from aiogram import Bot, F, Router
from aiogram.filters import Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

from apps.accounts.models import TelegramUser

from .. import services
from ..keyboards import BTN_ADMIN, MENU_BUTTONS, cancel_kb, main_menu
from ..render import esc, fmt_date
from ..sending import retry
from ..states import AdminSG


class IsSuperadmin(Filter):
    async def __call__(self, event: TelegramObject, user: TelegramUser | None = None) -> bool:
        return bool(user and user.is_superadmin)


router = Router()
router.message.filter(IsSuperadmin())
router.callback_query.filter(IsSuperadmin())

STATUS_ICON = {"active": "✅", "pending": "⏳", "blocked": "⛔"}


def _admin_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Пользователи", callback_data="adm:users:0")
    kb.button(text="⚠️ Проблемные ящики", callback_data="adm:errors")
    kb.button(text="📣 Рассылка", callback_data="adm:broadcast")
    kb.button(text="🔄 Обновить", callback_data="adm:home")
    kb.adjust(2, 2)
    return kb.as_markup()


async def _home_text() -> str:
    s = await services.admin_stats()
    u = s["users"]
    return (
        "🛡 <b>Панель суперадмина</b>\n\n"
        f"👥 Пользователи: ✅ {u.get('active', 0)} · ⏳ {u.get('pending', 0)} · ⛔ {u.get('blocked', 0)}\n"
        f"📬 Ящиков: {s['accounts']} (с ошибками: {s['accounts_err']})\n"
        f"📥 Входящих сегодня: {s['in_today']}\n"
        f"📤 Отправлено сегодня: {s['out_today']} · ошибок: {s['out_failed']} · в очереди: {s['queue']}"
    )


@router.message(F.text == BTN_ADMIN)
@router.message(Command("admin"))
async def admin_home(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(await _home_text(), reply_markup=_admin_kb())


@router.callback_query(F.data == "adm:home")
async def admin_home_cb(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(await _home_text(), reply_markup=_admin_kb())


@router.callback_query(F.data.startswith("adm:users:"))
async def users_list(callback: CallbackQuery):
    page = int(callback.data.split(":")[2])
    users, has_next = await services.users_page(page)
    kb = InlineKeyboardBuilder()
    for u in users:
        crown = "👑 " if u.is_superadmin else ""
        kb.button(text=f"{STATUS_ICON.get(u.status, '')} {crown}{u.full_name or u.username or u.telegram_id}"[:60],
                  callback_data=f"adm:u:{u.pk}")
    nav = 0
    if page:
        kb.button(text="◀️", callback_data=f"adm:users:{page - 1}")
        nav += 1
    if has_next:
        kb.button(text="▶️", callback_data=f"adm:users:{page + 1}")
        nav += 1
    kb.button(text="🏠 Назад", callback_data="adm:home")
    kb.adjust(*([1] * len(users)), *([nav] if nav else []), 1)
    await callback.answer()
    await callback.message.edit_text(f"👥 <b>Пользователи</b> · стр. {page + 1}", reply_markup=kb.as_markup())


async def _user_card(user_id: int):
    u, account = await services.user_detail(user_id)
    lines = [
        f"👤 <b>{esc(u.full_name) or '—'}</b> {'👑' if u.is_superadmin else ''}",
        f"🔗 @{esc(u.username)}" if u.username else "🔗 —",
        f"🆔 <code>{u.telegram_id}</code>",
        f"Статус: {STATUS_ICON.get(u.status, '')} {u.get_status_display()}",
        f"Зарегистрирован: {fmt_date(u.created_at)}",
        f"Активность: {fmt_date(u.last_seen_at)}",
    ]
    if account:
        lines += ["", f"📬 {esc(account.email)}", f"Синхронизация: {fmt_date(account.last_sync_at)}"]
        if account.last_error:
            lines.append(f"⚠️ <code>{esc(account.last_error[:300])}</code>")
    else:
        lines.append("\n📭 Почта не подключена")
    kb = InlineKeyboardBuilder()
    if not u.is_superadmin:
        if u.status != "active":
            kb.button(text="✅ Открыть доступ", callback_data=f"adm:set:{u.pk}:active")
        if u.status != "blocked":
            kb.button(text="⛔ Заблокировать", callback_data=f"adm:set:{u.pk}:blocked")
    kb.button(text="◀️ К списку", callback_data="adm:users:0")
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


@router.callback_query(F.data.startswith("adm:u:"))
async def user_card(callback: CallbackQuery):
    text, markup = await _user_card(int(callback.data.split(":")[2]))
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("adm:set:"))
async def set_status(callback: CallbackQuery, bot: Bot):
    _, _, user_id, status = callback.data.split(":")
    target, _ = await services.user_detail(int(user_id))
    if target.is_superadmin:
        return await callback.answer("Нельзя изменить суперадмина", show_alert=True)
    target = await services.update_user(target.pk, status=status)
    await callback.answer("Сохранено")
    text, markup = await _user_card(target.pk)
    await callback.message.edit_text(text, reply_markup=markup)
    try:
        if status == "active":
            await bot.send_message(target.telegram_id, "✅ Доступ к боту открыт! Нажмите /start, затем подключите почту.",
                                   reply_markup=main_menu(False))
        elif status == "blocked":
            await bot.send_message(target.telegram_id, "⛔ Доступ к боту закрыт администратором.")
    except Exception:
        pass


@router.callback_query(F.data == "adm:errors")
async def problem_accounts(callback: CallbackQuery):
    accounts = await services.problem_accounts()
    await callback.answer()
    if not accounts:
        text = "✅ Все ящики работают без ошибок."
    else:
        rows = []
        for a in accounts:
            flag = "🔐" if a.needs_reauth else ("⏸" if not a.is_active else "⚠️")
            rows.append(f"{flag} <b>{esc(a.email)}</b> ({esc(a.owner.full_name)})\n"
                        f"   {fmt_date(a.last_sync_at)} · <code>{esc(a.last_error[:150]) or '—'}</code>")
        text = "⚠️ <b>Проблемные ящики</b>\n\n" + "\n".join(rows)
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Назад", callback_data="adm:home")
    await callback.message.edit_text(text[:4000], reply_markup=kb.as_markup())


@router.callback_query(F.data == "adm:broadcast")
async def ask_broadcast(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSG.broadcast)
    await callback.answer()
    await callback.message.answer("📣 Отправьте текст рассылки для всех активных пользователей:",
                                  reply_markup=cancel_kb())


@router.message(AdminSG.broadcast, F.text, ~F.text.in_(MENU_BUTTONS))
async def do_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    chat_ids = await services.active_chat_ids()
    ok = 0
    for chat_id in chat_ids:
        try:
            await retry(bot.send_message, chat_id, f"📣 <b>Сообщение администратора</b>\n\n{esc(message.text)}")
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await message.answer(f"✅ Рассылка отправлена: {ok} из {len(chat_ids)}")
