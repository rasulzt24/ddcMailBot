"""Новое письмо, ответ, ответ всем, пересылка. Черновик хранится в FSM (Redis)."""
import asyncio
import io
import logging
from collections import defaultdict

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from django.core.exceptions import ObjectDoesNotExist

from apps.accounts.models import TelegramUser
from apps.mail.models import OutgoingMessage
from apps.mail.services.compose import dedupe

from .. import services
from ..keyboards import (BTN_COMPOSE, MENU_BUTTONS, REAUTH_BANNER, cancel_kb, draft_kb, pick_contacts_kb,
                         reauth_kb, recipients_kb, skip_kb)
from ..render import esc, fmt_size, trim
from ..states import ComposeSG

router = Router()
logger = logging.getLogger(__name__)

MODE = OutgoingMessage.Mode
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API отдаёт боту файлы до 20 МБ
MODE_TITLES = {MODE.NEW: "✉️ Новое письмо", MODE.REPLY: "↩️ Ответ", MODE.REPLY_ALL: "↩️ Ответ всем",
               MODE.FORWARD: "↪️ Пересылка"}

# Альбом из нескольких файлов приходит параллельными апдейтами — сериализуем правку черновика
_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


async def _draft(state: FSMContext) -> dict:
    return (await state.get_data()).get("draft") or {}


async def _save(state: FSMContext, draft: dict):
    await state.update_data(draft=draft)


# ---------- Точки входа ----------

async def _start(message: Message, state: FSMContext, user: TelegramUser, mode: str, source_id: int | None):
    account = await services.get_account(user)
    if not account:
        await message.answer("📭 Сначала подключите почту: «📬 Моя почта».")
        return
    if account.needs_reauth:
        await message.answer(REAUTH_BANNER, reply_markup=reauth_kb())
        return
    try:
        draft = await services.new_draft(user, mode, source_id)
    except ObjectDoesNotExist:
        await message.answer("Письмо не найдено.")
        return
    await state.clear()
    await state.update_data(draft=draft, editing=False)
    if mode in (MODE.REPLY, MODE.REPLY_ALL):
        await ask_body(message, state, draft)
    else:
        await ask_recipients(message, state, user, "to")


@router.message(F.text == BTN_COMPOSE)
@router.message(Command("new"))
async def compose_new(message: Message, state: FSMContext, user: TelegramUser):
    await _start(message, state, user, MODE.NEW, None)


@router.callback_query(F.data.regexp(r"^m:(r|ra|f):\d+$"))
async def compose_from_message(callback: CallbackQuery, state: FSMContext, user: TelegramUser):
    _, action, message_id = callback.data.split(":")
    mode = {"r": MODE.REPLY, "ra": MODE.REPLY_ALL, "f": MODE.FORWARD}[action]
    await callback.answer()
    await _start(callback.message, state, user, mode, int(message_id))


@router.message(StateFilter(None), F.reply_to_message, F.text | F.caption)
async def quick_reply(message: Message, state: FSMContext, user: TelegramUser, bot: Bot):
    """Reply на карточку письма в Telegram = ответ на письмо."""
    source_id = await services.message_by_telegram(message.chat.id, message.reply_to_message.message_id, user)
    if not source_id:
        await message.answer("ℹ️ Чтобы ответить на письмо, ответьте (reply) на его карточку или нажмите «↩️ Ответить».")
        return
    draft = await services.new_draft(user, MODE.REPLY, source_id)
    draft["body"] = message.text or message.caption or ""
    file = _file_from_message(message)
    if file:
        draft["files"].append(file)
    await state.clear()
    await state.update_data(draft=draft, editing=False)
    await show_confirm(message, state)


# ---------- Получатели ----------

async def ask_recipients(message: Message, state: FSMContext, user: TelegramUser, field: str):
    draft = await _draft(state)
    await state.set_state(ComposeSG.to if field == "to" else ComposeSG.cc)
    current = draft.get(field, [])
    contacts = await services.frequent_contacts(user, draft.get("to", []) + draft.get("cc", []))
    title = "👥 <b>Кому?</b>" if field == "to" else "📋 <b>Копия (CC)?</b>"
    text = (f"{title}\nВведите e-mail или имя/фамилию из адресной книги. Несколько — через запятую.\n")
    if current:
        text += "\n<b>Уже добавлены:</b>\n" + "\n".join(f"• {esc(a)}" for a in current)
    if contacts:
        text += "\n\n<i>Частые адресаты — кнопками ниже.</i>"
    await message.answer(text, reply_markup=recipients_kb(field, contacts, bool(current), optional=field == "cc"))


def _field_by_state(state_name: str | None) -> str:
    return "cc" if state_name == ComposeSG.cc.state else "to"


def _add_unique(items: list[str], new: list[str]) -> list[str]:
    return dedupe(items + new)


@router.message(StateFilter(ComposeSG.to, ComposeSG.cc), F.text, ~F.text.in_(MENU_BUTTONS))
async def got_recipients(message: Message, state: FSMContext, user: TelegramUser):
    field = _field_by_state(await state.get_state())
    resolved, unknown, ambiguous = await services.resolve(user, message.text)
    draft = await _draft(state)
    draft[field] = _add_unique(draft.get(field, []), resolved)
    await _save(state, draft)
    if unknown:
        await message.answer("⚠️ Не найдено в адресной книге и не похоже на e-mail: "
                             + ", ".join(f"<code>{esc(u)}</code>" for u in unknown))
    for token, contacts in ambiguous.items():
        await message.answer(f"❓ «{esc(token)}» — несколько совпадений, выберите:",
                             reply_markup=pick_contacts_kb(field, contacts))
    await ask_recipients(message, state, user, field)


@router.callback_query(F.data.startswith("cp:"))
async def pick_contact(callback: CallbackQuery, state: FSMContext, user: TelegramUser):
    _, field, contact_id = callback.data.split(":")
    address = await services.contact_address(user, int(contact_id))
    draft = await _draft(state)
    if not address or not draft:
        return await callback.answer("Черновик не найден", show_alert=True)
    draft[field] = _add_unique(draft.get(field, []), [address])
    await _save(state, draft)
    await callback.answer(f"Добавлено: {address}"[:190])
    await ask_recipients(callback.message, state, user, field)


@router.callback_query(F.data.startswith("cclear:"))
async def clear_recipients(callback: CallbackQuery, state: FSMContext, user: TelegramUser):
    field = callback.data.split(":")[1]
    draft = await _draft(state)
    draft[field] = []
    await _save(state, draft)
    await callback.answer("Очищено")
    await ask_recipients(callback.message, state, user, field)


@router.callback_query(F.data.startswith("cn:"))
async def next_step(callback: CallbackQuery, state: FSMContext, user: TelegramUser):
    step = callback.data.split(":")[1]
    data = await state.get_data()
    draft, editing = data.get("draft"), data.get("editing")
    if not draft:
        return await callback.answer("Черновик не найден", show_alert=True)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    if step == "to":
        if not draft["to"]:
            return await callback.message.answer("Добавьте хотя бы одного получателя.")
        if editing:
            return await show_confirm(callback.message, state)
        return await ask_recipients(callback.message, state, user, "cc")
    if step == "cc":
        if editing:
            return await show_confirm(callback.message, state)
        if draft["mode"] == MODE.NEW:
            return await ask_subject(callback.message, state)
        return await ask_body(callback.message, state, draft)
    if step == "body":  # пересылка без комментария
        return await ask_files(callback.message, state, draft)
    if step == "files":
        return await show_confirm(callback.message, state)


# ---------- Тема и текст ----------

async def ask_subject(message: Message, state: FSMContext):
    await state.set_state(ComposeSG.subject)
    await message.answer("📌 <b>Тема письма:</b>", reply_markup=skip_kb("d:nosubject", "⏭ Без темы"))


@router.message(ComposeSG.subject, F.text, ~F.text.in_(MENU_BUTTONS))
async def got_subject(message: Message, state: FSMContext):
    draft = await _draft(state)
    draft["subject"] = message.text.strip()[:900]
    await _save(state, draft)
    if (await state.get_data()).get("editing"):
        return await show_confirm(message, state)
    await ask_body(message, state, draft)


@router.callback_query(F.data == "d:nosubject")
async def no_subject(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    draft = await _draft(state)
    draft["subject"] = ""
    await _save(state, draft)
    if (await state.get_data()).get("editing"):
        return await show_confirm(callback.message, state)
    await ask_body(callback.message, state, draft)


async def ask_body(message: Message, state: FSMContext, draft: dict):
    await state.set_state(ComposeSG.body)
    if draft["mode"] in (MODE.REPLY, MODE.REPLY_ALL):
        to = ", ".join(draft["to"]) or "—"
        text = (f"{MODE_TITLES[draft['mode']]} на «{esc(draft.get('source_subject'))}»\n"
                f"📨 Кому: {esc(to)}\n" + (f"📋 Копия: {esc(', '.join(draft['cc']))}\n" if draft["cc"] else "")
                + "\n✍️ <b>Напишите текст ответа</b> (можно сразу с файлом). Исходное письмо будет процитировано.")
        await message.answer(text, reply_markup=cancel_kb())
    elif draft["mode"] == MODE.FORWARD:
        await message.answer("✍️ <b>Комментарий к пересылаемому письму</b> (или пропустите):",
                             reply_markup=skip_kb("cn:body", "⏭ Без комментария"))
    else:
        await message.answer("✍️ <b>Текст письма</b> (можно сразу с файлом):", reply_markup=cancel_kb())


@router.message(ComposeSG.body, ~F.text.in_(MENU_BUTTONS))
async def got_body(message: Message, state: FSMContext):
    text = message.text or message.caption
    file = _file_from_message(message)
    if not text and not file:
        return await message.answer("Отправьте текст письма.")
    async with _locks[message.from_user.id]:
        draft = await _draft(state)
        if text:
            draft["body"] = text
        if file:
            draft["files"].append(file)
        await _save(state, draft)
    if (await state.get_data()).get("editing"):
        return await show_confirm(message, state)
    await ask_files(message, state, draft)


# ---------- Файлы ----------

def _file_from_message(message: Message) -> dict | None:
    if message.document:
        d = message.document
        return {"file_id": d.file_id, "name": d.file_name or "file", "mime": d.mime_type or "", "size": d.file_size or 0}
    if message.photo:
        p = message.photo[-1]
        return {"file_id": p.file_id, "name": f"photo_{p.file_unique_id}.jpg", "mime": "image/jpeg",
                "size": p.file_size or 0}
    if message.video:
        v = message.video
        return {"file_id": v.file_id, "name": v.file_name or f"video_{v.file_unique_id}.mp4",
                "mime": v.mime_type or "video/mp4", "size": v.file_size or 0}
    if message.audio:
        a = message.audio
        return {"file_id": a.file_id, "name": a.file_name or f"audio_{a.file_unique_id}.mp3",
                "mime": a.mime_type or "audio/mpeg", "size": a.file_size or 0}
    if message.voice:
        v = message.voice
        return {"file_id": v.file_id, "name": f"voice_{v.file_unique_id}.ogg", "mime": v.mime_type or "audio/ogg",
                "size": v.file_size or 0}
    return None


async def ask_files(message: Message, state: FSMContext, draft: dict):
    await state.set_state(ComposeSG.files)
    count = len(draft.get("files", []))
    text = "📎 <b>Прикрепите файлы</b> (документы, фото, видео) — или нажмите кнопку."
    if count:
        text += f"\nУже прикреплено: {count}"
    await message.answer(text, reply_markup=skip_kb("cn:files", "✅ Готово" if count else "⏭ Без вложений"))


@router.message(ComposeSG.files, F.document | F.photo | F.video | F.audio | F.voice)
async def got_file(message: Message, state: FSMContext):
    file = _file_from_message(message)
    if file["size"] > TG_DOWNLOAD_LIMIT:
        return await message.reply(f"⚠️ «{esc(file['name'])}» больше 20 МБ — Telegram не даёт боту скачать такой файл.")
    async with _locks[message.from_user.id]:
        draft = await _draft(state)
        draft["files"].append(file)
        if message.caption and not draft.get("body"):
            draft["body"] = message.caption
        await _save(state, draft)
        count = len(draft["files"])
    await message.reply(f"📎 Добавлено: <b>{esc(file['name'])}</b> (всего {count})",
                        reply_markup=skip_kb("cn:files", "✅ Готово"))


@router.message(ComposeSG.files, F.text, ~F.text.in_(MENU_BUTTONS))
async def files_text(message: Message, state: FSMContext):
    await message.answer("Прикрепите файл или нажмите «✅ Готово».", reply_markup=skip_kb("cn:files", "✅ Готово"))


# ---------- Предпросмотр и отправка ----------

async def show_confirm(message: Message, state: FSMContext):
    await state.set_state(ComposeSG.confirm)
    await state.update_data(editing=False)
    draft = await _draft(state)
    files = draft.get("files", [])
    lines = [f"📝 <b>Черновик</b> · {MODE_TITLES.get(draft['mode'], '')}", ""]
    lines.append(f"📨 <b>Кому:</b> {esc(', '.join(draft['to'])) or '<i>не указано</i>'}")
    if draft.get("cc"):
        lines.append(f"📋 <b>Копия:</b> {esc(', '.join(draft['cc']))}")
    lines.append(f"📌 <b>Тема:</b> {esc(draft.get('subject')) or '<i>(без темы)</i>'}")
    if files:
        lines.append("📎 <b>Файлы:</b> " + ", ".join(f"{esc(f['name'])} ({fmt_size(f['size'])})" for f in files))
    if draft.get("source_files") and draft["mode"] == MODE.FORWARD:
        state_txt = "будут приложены" if draft.get("include_attachments") else "не прикладываются"
        lines.append(f"📎 Вложения исходного письма ({draft['source_files']}): {state_txt}")
    body, _ = trim(draft.get("body", ""), 2500)
    lines.append("")
    lines.append(f"<blockquote expandable>{esc(body) or '<i>(без текста)</i>'}</blockquote>")
    if draft.get("source_id"):
        lines.append("<i>+ исходное письмо будет процитировано ниже</i>")
    await message.answer("\n".join(lines),
                         reply_markup=draft_kb(draft["mode"], draft.get("include_attachments", False),
                                               draft.get("source_files", 0) if draft["mode"] == MODE.FORWARD else 0))


@router.callback_query(F.data.startswith("d:edit:"))
async def edit_field(callback: CallbackQuery, state: FSMContext, user: TelegramUser):
    field = callback.data.split(":")[2]
    draft = await _draft(state)
    if not draft:
        return await callback.answer("Черновик не найден", show_alert=True)
    await callback.answer()
    await state.update_data(editing=True)
    if field in ("to", "cc"):
        await ask_recipients(callback.message, state, user, field)
    elif field == "subject":
        await ask_subject(callback.message, state)
    elif field == "body":
        await state.set_state(ComposeSG.body)
        await callback.message.answer("✍️ Отправьте новый текст письма:")
    elif field == "files":
        draft["files"] = []
        await _save(state, draft)
        await state.update_data(editing=False)
        await callback.message.answer("🧹 Прикреплённые файлы очищены.")
        await ask_files(callback.message, state, draft)


@router.callback_query(F.data == "d:toggle_att")
async def toggle_attachments(callback: CallbackQuery, state: FSMContext):
    draft = await _draft(state)
    if not draft:
        return await callback.answer("Черновик не найден", show_alert=True)
    draft["include_attachments"] = not draft.get("include_attachments")
    await _save(state, draft)
    await callback.answer("Вложения оригинала: " + ("вкл" if draft["include_attachments"] else "выкл"))
    await callback.message.delete()
    await show_confirm(callback.message, state)


@router.callback_query(ComposeSG.confirm, F.data == "d:send")
async def send(callback: CallbackQuery, state: FSMContext, user: TelegramUser, bot: Bot):
    draft = await _draft(state)
    if not draft.get("to"):
        return await callback.answer("Укажите получателя", show_alert=True)
    await callback.answer("⏳ Отправляю…")
    await callback.message.edit_reply_markup(reply_markup=None)

    files = []
    for f in draft.get("files", []):
        buf = io.BytesIO()
        try:
            await bot.download(f["file_id"], destination=buf)
        except Exception as e:
            logger.warning("download %s failed: %s", f["name"], e)
            await callback.message.answer(f"⚠️ Не удалось скачать «{esc(f['name'])}»: {esc(e)}. "
                                          f"Черновик сохранён — попробуйте ещё раз.",
                                          reply_markup=draft_kb(draft["mode"], draft.get("include_attachments", False),
                                                                draft.get("source_files", 0)))
            return
        files.append((f["name"], f["mime"], buf.getvalue()))

    await services.queue_outgoing(user, draft, files)
    await state.clear()
    await callback.message.answer("📤 Письмо поставлено в очередь на отправку. Сообщу, когда уйдёт.")


@router.callback_query(F.data == "d:send")
async def send_stale(callback: CallbackQuery):
    await callback.answer("Черновик устарел — начните заново", show_alert=True)
