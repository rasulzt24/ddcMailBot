"""«⏱ Рабочий день»: начать/завершить день в Битриксе сейчас или с указанным временем, напоминания."""
import logging
import re
from datetime import datetime, time, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from django.utils import timezone

from apps.accounts.models import TelegramUser

from .. import workday
from ..keyboards import BTN_WORKDAY, MENU_BUTTONS, cancel_kb
from ..render import esc
from ..states import WorkdaySG

router = Router()
logger = logging.getLogger(__name__)

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
START_PRESETS = ["08:30", "08:45", "09:00", "09:30"]
END_PRESETS = ["17:30", "18:00", "18:30", "19:00"]
TIME_RE = re.compile(r"^\s*([01]?\d|2[0-3])[:.\s]?([0-5]\d)\s*$")


def _fmt_duration(seconds: int) -> str:
    h, m = divmod(max(seconds, 0) // 60, 60)
    return f"{h} ч {m:02d} мин" if h else f"{m} мин"


def _hm(dt: datetime | None) -> str:
    return dt.strftime("%H:%M") if dt else "—"


def parse_time(text: str) -> time | None:
    m = TIME_RE.match(text or "")
    return time(int(m.group(1)), int(m.group(2))) if m else None


# ---------- Экран статуса ----------

def _status_view(st: workday.DayState, prof) -> tuple[str, object]:
    today = timezone.localdate()
    lines = [f"⏱ <b>Рабочий день</b> · {WEEKDAYS[today.weekday()]}, {today:%d.%m.%Y}", ""]
    kb = InlineKeyboardBuilder()
    if st.unclosed_previous_day:
        lines.append(f"⚠️ <b>Не закрыт день за {st.start:%d.%m}</b> (начат в {_hm(st.start)}).\n"
                     f"Сначала завершите его, указав время окончания.")
        kb.button(text=f"⏹ Завершить день за {st.start:%d.%m} в…", callback_data="wd:at:close")
    elif st.is_open:
        worked = int((timezone.now() - st.start).total_seconds()) if st.start else st.duration
        icon = "⏸ На паузе" if st.state == "PAUSED" else "🟢 Идёт"
        lines.append(f"{icon} с <b>{_hm(st.start)}</b> · {_fmt_duration(worked)}")
        kb.button(text="⏹ Завершить сейчас", callback_data="wd:now:close")
        kb.button(text="🕕 Завершить в…", callback_data="wd:at:close")
    elif st.finished_today:
        lines.append(f"✅ Завершён: <b>{_hm(st.start)} – {_hm(st.finish)}</b> · {_fmt_duration(st.duration)}")
    else:
        lines.append("⚪ Ещё не начат")
        kb.button(text="▶️ Начать сейчас", callback_data="wd:now:open")
        kb.button(text="🕘 Начать в…", callback_data="wd:at:open")

    reminders = []
    if prof.remind_start:
        reminders.append(f"начать в {prof.remind_start:%H:%M}")
    if prof.remind_end:
        reminders.append(f"завершить в {prof.remind_end:%H:%M}")
    lines.append("")
    lines.append(f"⏰ Напоминания (по будням): {', '.join(reminders)}" if reminders else "⏰ Напоминания выключены")
    kb.button(text="🔄 Обновить", callback_data="wd:refresh")
    kb.button(text="⏰ Напоминания", callback_data="wd:remind")
    kb.adjust(*([1] if st.unclosed_previous_day else [2]), 2)
    return "\n".join(lines), kb.as_markup()


async def _ask_credentials(message: Message, state: FSMContext, reason: str = ""):
    await state.set_state(WorkdaySG.login)
    text = "🔐 <b>Вход в Битрикс</b>\n"
    text += (reason + "\n") if reason else ""
    text += "Отправьте логин от портала (как на странице входа, например <code>Ivan.Ivanov</code>):"
    await message.answer(text, reply_markup=cancel_kb())


async def show_status(message: Message, user: TelegramUser, state: FSMContext, edit: bool = False):
    try:
        st = await workday.call(user, "status")
    except workday.NeedCredentials:
        prof = await workday.profile(user)
        reason = ("Битрикс не принял логин или пароль — возможно, пароль сменился."
                  if prof.auth_failed_at else "Не нашёл данных для входа.")
        return await _ask_credentials(message, state, reason)
    except Exception as e:
        logger.warning("Bitrix status failed: %s", e)
        return await message.answer(f"⚠️ Битрикс недоступен: <code>{esc(e)}</code>\nПроверьте VPN и повторите.")
    prof = await workday.profile(user)
    text, markup = _status_view(st, prof)
    if edit:
        try:
            return await message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
    await message.answer(text, reply_markup=markup)


@router.message(F.text == BTN_WORKDAY)
@router.message(Command("workday"))
async def workday_menu(message: Message, state: FSMContext, user: TelegramUser):
    await state.clear()
    wait = await message.answer("⏳ Получаю статус из Битрикса…")
    await show_status(message, user, state)
    try:
        await wait.delete()
    except Exception:
        pass


@router.callback_query(F.data == "wd:refresh")
async def refresh(callback: CallbackQuery, state: FSMContext, user: TelegramUser):
    await callback.answer("Обновляю…")
    await show_status(callback.message, user, state, edit=True)


# ---------- Начать / завершить ----------

async def _do(message: Message, state: FSMContext, user: TelegramUser, action: str, at: time):
    """Выполнить open/close в Битриксе с проверками и показать новый статус."""
    try:
        st = await workday.call(user, "status")
        now = timezone.localtime()
        if action == "open":
            if st.is_open:
                return await message.answer("ℹ️ Рабочий день уже идёт.")
            if at > now.time():
                return await message.answer("⚠️ Нельзя начать день в будущем времени.")
            result = await workday.call(user, "open", at, st.record_id)
        else:
            if not st.is_open:
                return await message.answer("ℹ️ Рабочий день не начат — завершать нечего.")
            same_day = st.start and st.start.date() == now.date()
            if same_day and at > now.time():
                return await message.answer("⚠️ Нельзя завершить день в будущем времени.")
            if st.start and at <= st.start.time() and same_day:
                return await message.answer(f"⚠️ Время окончания должно быть позже начала ({_hm(st.start)}).")
            result = await workday.call(user, "close", at, st.record_id)
    except workday.NeedCredentials:
        return await _ask_credentials(message, state, "Битрикс не принял логин или пароль.")
    except Exception as e:
        logger.warning("Bitrix %s failed: %s", action, e)
        await workday.log(user, action, at, ok=False, error=str(e))
        return await message.answer(f"❌ Битрикс отказал: <code>{esc(e)}</code>")
    await workday.log(user, action, at, ok=True)
    done = "▶️ Рабочий день начат" if action == "open" else "⏹ Рабочий день завершён"
    started = f" в {_hm(result.start)}" if action == "open" else f" в {_hm(result.finish)}"
    await message.answer(f"{done}{started}.")
    await state.clear()
    await show_status(message, user, state)


@router.callback_query(F.data.startswith("wd:now:"))
async def do_now(callback: CallbackQuery, state: FSMContext, user: TelegramUser):
    action = callback.data.split(":")[2]
    await callback.answer("⏳ Отправляю в Битрикс…")
    await _do(callback.message, state, user, action, timezone.localtime().time().replace(second=0, microsecond=0))


def _presets(action: str) -> list[str]:
    """Быстрые варианты: не позже текущего времени (кроме закрытия вчерашнего дня)."""
    now = timezone.localtime()
    presets = START_PRESETS if action == "open" else END_PRESETS
    allowed = [p for p in presets if parse_time(p) <= now.time()]
    # Шаг 15 минут назад от текущего времени — частый случай «забыл отметиться»
    rounded = (now - timedelta(minutes=now.minute % 15)).replace(second=0, microsecond=0)
    for k in range(3):
        t = (rounded - timedelta(minutes=15 * k)).strftime("%H:%M")
        if t not in allowed:
            allowed.append(t)
    return sorted(set(allowed))[-6:]


@router.callback_query(F.data.startswith("wd:at:"))
async def ask_time(callback: CallbackQuery, state: FSMContext, user: TelegramUser):
    action = callback.data.split(":")[2]
    await callback.answer()
    await state.set_state(WorkdaySG.time)
    await state.update_data(wd_action=action)
    try:
        st = await workday.call(user, "status")
        previous = st.unclosed_previous_day
    except Exception:
        previous = False
    presets = END_PRESETS if previous else _presets(action)
    kb = InlineKeyboardBuilder()
    for p in presets:
        kb.button(text=p, callback_data=f"wd:t:{p.replace(':', '')}")
    kb.button(text="❌ Отмена", callback_data="d:cancel")
    kb.adjust(3, 3, 1)
    what = "начала" if action == "open" else "окончания"
    await callback.message.answer(f"🕘 Время {what} рабочего дня — выберите или отправьте в формате <b>ЧЧ:ММ</b>:",
                                  reply_markup=kb.as_markup())


@router.callback_query(WorkdaySG.time, F.data.startswith("wd:t:"))
async def picked_time(callback: CallbackQuery, state: FSMContext, user: TelegramUser):
    raw = callback.data.split(":")[2]
    action = (await state.get_data()).get("wd_action", "open")
    await callback.answer("⏳ Отправляю в Битрикс…")
    await _do(callback.message, state, user, action, time(int(raw[:2]), int(raw[2:])))


@router.message(WorkdaySG.time, F.text, ~F.text.in_(MENU_BUTTONS))
async def typed_time(message: Message, state: FSMContext, user: TelegramUser):
    at = parse_time(message.text)
    if not at:
        return await message.answer("Не понял время. Пример: <b>08:55</b>", reply_markup=cancel_kb())
    action = (await state.get_data()).get("wd_action", "open")
    await _do(message, state, user, action, at)


# ---------- Вход в Битрикс ----------

@router.message(WorkdaySG.login, F.text, ~F.text.in_(MENU_BUTTONS))
async def got_login(message: Message, state: FSMContext):
    await state.update_data(wd_login=message.text.strip())
    await state.set_state(WorkdaySG.password)
    await message.answer("🔑 Теперь пароль от портала.\n<i>Сообщение с паролем будет сразу удалено.</i>",
                         reply_markup=cancel_kb())


@router.message(WorkdaySG.password, F.text, ~F.text.in_(MENU_BUTTONS))
async def got_password(message: Message, state: FSMContext, user: TelegramUser):
    password = message.text
    try:
        await message.delete()
    except Exception:
        pass
    login = (await state.get_data()).get("wd_login", "")
    await workday.save_credentials(user, login, password)
    await state.clear()
    await message.answer("⏳ Проверяю вход в Битрикс…")
    await show_status(message, user, state)


# ---------- Напоминания ----------

def _reminder_kb(prof):
    kb = InlineKeyboardBuilder()
    for which, presets, label in (("start", START_PRESETS, "▶️ Начать"), ("end", END_PRESETS, "⏹ Завершить")):
        current = prof.remind_start if which == "start" else prof.remind_end
        for p in presets:
            mark = "✅ " if current and current.strftime("%H:%M") == p else ""
            kb.button(text=f"{mark}{p}", callback_data=f"wd:r:{which}:{p.replace(':', '')}")
        kb.button(text=("✅ " if not current else "") + "Выкл", callback_data=f"wd:r:{which}:off")
        kb.button(text="✏️ Своё время", callback_data=f"wd:r:{which}:custom")
    kb.button(text="◀️ Назад", callback_data="wd:refresh")
    kb.adjust(4, 2, 4, 2, 1)
    return kb.as_markup()


def _reminder_text(prof) -> str:
    start = prof.remind_start.strftime("%H:%M") if prof.remind_start else "выкл"
    end = prof.remind_end.strftime("%H:%M") if prof.remind_end else "выкл"
    return ("⏰ <b>Напоминания по будням</b>\n\n"
            f"▶️ Начать день: <b>{start}</b> — если день ещё не начат\n"
            f"⏹ Завершить день: <b>{end}</b> — если день ещё идёт\n\n"
            "В напоминании будет кнопка — отметиться можно одним нажатием.\n"
            "Первый ряд — время напоминания о начале, второй — о завершении.")


@router.callback_query(F.data == "wd:remind")
async def reminders(callback: CallbackQuery, user: TelegramUser):
    await callback.answer()
    prof = await workday.profile(user)
    await callback.message.edit_text(_reminder_text(prof), reply_markup=_reminder_kb(prof))


@router.callback_query(F.data.startswith("wd:r:"))
async def set_reminder(callback: CallbackQuery, state: FSMContext, user: TelegramUser):
    _, _, which, value = callback.data.split(":")
    if value == "custom":
        await state.set_state(WorkdaySG.remind_time)
        await state.update_data(wd_remind=which)
        await callback.answer()
        what = "начать" if which == "start" else "завершить"
        return await callback.message.answer(f"✏️ Во сколько напоминать {what} день? Формат <b>ЧЧ:ММ</b>:",
                                             reply_markup=cancel_kb())
    at = None if value == "off" else time(int(value[:2]), int(value[2:]))
    prof = await workday.save_reminder(user, which, at)
    await callback.answer("Сохранено")
    await callback.message.edit_text(_reminder_text(prof), reply_markup=_reminder_kb(prof))


@router.message(WorkdaySG.remind_time, F.text, ~F.text.in_(MENU_BUTTONS))
async def typed_reminder(message: Message, state: FSMContext, user: TelegramUser):
    at = parse_time(message.text)
    if not at:
        return await message.answer("Не понял время. Пример: <b>09:00</b>", reply_markup=cancel_kb())
    which = (await state.get_data()).get("wd_remind", "start")
    prof = await workday.save_reminder(user, which, at)
    await state.clear()
    await message.answer(_reminder_text(prof), reply_markup=_reminder_kb(prof))
