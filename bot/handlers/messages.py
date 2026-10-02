"""Входящие / отправленные / поиск и действия с письмом."""
import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from django.core.exceptions import ObjectDoesNotExist

from apps.accounts.models import TelegramUser
from apps.mail.services.imap import ImapSession

from .. import services
from ..keyboards import BTN_INBOX, BTN_SEARCH, BTN_SENT, cancel_kb, full_view_actions
from ..render import esc
from ..sending import send_attachments, send_card, send_long_text
from ..states import SearchSG

router = Router()
logger = logging.getLogger(__name__)


# ---------- Списки ----------

@router.message(F.text == BTN_INBOX)
@router.message(Command("inbox"))
async def inbox(message: Message, state: FSMContext, user: TelegramUser):
    await state.clear()
    text, markup = await services.message_list(user, "in", 0)
    await message.answer(text, reply_markup=markup)


@router.message(F.text == BTN_SENT)
@router.message(Command("sent"))
async def sent(message: Message, state: FSMContext, user: TelegramUser):
    await state.clear()
    text, markup = await services.message_list(user, "out", 0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("box:"))
async def box_page(callback: CallbackQuery, user: TelegramUser):
    _, direction, page = callback.data.split(":")
    await callback.answer()
    text, markup = await services.message_list(user, direction, int(page))
    await callback.message.edit_text(text, reply_markup=markup)


@router.message(F.text == BTN_SEARCH)
@router.message(Command("search"))
async def search(message: Message, state: FSMContext, user: TelegramUser, command: CommandObject | None = None):
    query = (command.args or "").strip() if command else ""
    if not query:
        await state.set_state(SearchSG.query)
        await message.answer("🔎 Что ищем? Тема, отправитель, получатель или текст письма:", reply_markup=cancel_kb())
        return
    await _do_search(message, state, user, query)


@router.message(SearchSG.query, F.text)
async def search_query(message: Message, state: FSMContext, user: TelegramUser):
    await _do_search(message, state, user, message.text.strip())


async def _do_search(message: Message, state: FSMContext, user: TelegramUser, query: str):
    await state.clear()
    await state.update_data(search_query=query)
    text, markup = await services.message_list(user, "", 0, query=query)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("srch:"))
async def search_page(callback: CallbackQuery, state: FSMContext, user: TelegramUser):
    query = (await state.get_data()).get("search_query", "")
    if not query:
        return await callback.answer("Поиск устарел, повторите", show_alert=True)
    await callback.answer()
    text, markup = await services.message_list(user, "", int(callback.data.split(":")[1]), query=query)
    await callback.message.edit_text(text, reply_markup=markup)


# ---------- Действия с письмом ----------

def _message_id(callback: CallbackQuery) -> int:
    return int(callback.data.rsplit(":", 1)[1])


@router.callback_query(F.data.startswith("m:o:"))
async def open_message(callback: CallbackQuery, bot: Bot, user: TelegramUser):
    await callback.answer()
    try:
        card = await services.card_for(_message_id(callback), user)
    except ObjectDoesNotExist:
        return await callback.message.answer("Письмо не найдено.")
    await send_card(bot, callback.message.chat.id, user, card, with_attachments=False)


@router.callback_query(F.data.startswith("m:full:"))
async def full_message(callback: CallbackQuery, bot: Bot, user: TelegramUser):
    message_id = _message_id(callback)
    await callback.answer()
    try:
        subject, body, has_html, has_raw = await services.full_text(message_id, user)
    except ObjectDoesNotExist:
        return await callback.message.answer("Письмо не найдено.")
    await send_long_text(bot, callback.message.chat.id, f"📄 <b>{esc(subject) or '(без темы)'}</b>",
                         body or "(пусто)", "letter.txt",
                         reply_markup=full_view_actions(message_id, has_html, has_raw))


@router.callback_query(F.data.startswith("m:html:"))
async def html_version(callback: CallbackQuery, user: TelegramUser):
    await callback.answer()
    subject, body = await services.html_body(_message_id(callback), user)
    await callback.message.answer_document(BufferedInputFile(body.encode("utf-8"), filename="letter.html"),
                                           caption=f"🌐 {esc(subject)[:900]}\n<i>Откройте в браузере — "
                                                   f"письмо с исходным оформлением</i>")


@router.callback_query(F.data.startswith("m:eml:"))
async def eml_file(callback: CallbackQuery, user: TelegramUser):
    await callback.answer("⏳ Загружаю оригинал…")
    subject, raw, imap_ref = await services.raw_source(_message_id(callback), user)
    if raw is None and imap_ref:
        try:
            raw = await asyncio.to_thread(_imap_fetch_raw, *imap_ref)
        except Exception as e:
            return await callback.message.answer(f"⚠️ Не удалось скачать оригинал с почтового сервера: {esc(e)}")
    if not raw:
        return await callback.message.answer("Оригинал письма недоступен.")
    await callback.message.answer_document(BufferedInputFile(raw, filename="message.eml"),
                                           caption=f"📦 {esc(subject)[:900]}")


@router.callback_query(F.data.startswith("m:h:"))
async def history(callback: CallbackQuery, user: TelegramUser):
    await callback.answer()
    try:
        text, markup = await services.thread_view(_message_id(callback), user)
    except ObjectDoesNotExist:
        return await callback.message.answer("Письмо не найдено.")
    await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("m:att:"))
@router.callback_query(F.data.startswith("m:img:"))
async def attachments(callback: CallbackQuery, bot: Bot, user: TelegramUser):
    inline = callback.data.startswith("m:img:")
    items = await services.attachments_of(_message_id(callback), user, inline=inline)
    if not items:
        return await callback.answer("Вложений нет", show_alert=True)
    await callback.answer(f"Отправляю {len(items)} файл(ов)…")
    await send_attachments(bot, callback.message.chat.id, items, reply_to=callback.message.message_id)


def _imap_fetch_raw(account, folder: str, uid: int) -> bytes | None:
    with ImapSession.for_account(account) as imap:
        imap.select(folder)
        raw, _ = imap.fetch(uid)
        return raw


def _imap_set_seen(account, uid: int, folder: str, seen: bool):
    with ImapSession.for_account(account) as imap:
        imap.set_seen(uid, seen, folder)


_seen_in_progress: set[int] = set()


async def _refresh_markup(message: Message, message_id: int, user: TelegramUser):
    card = await services.card_for(message_id, user)
    try:
        await message.edit_reply_markup(reply_markup=card.markup)
    except Exception:
        pass


async def _sync_seen_to_server(message: Message, message_id: int, user: TelegramUser, target, seen: bool):
    """Вход в IMAP занимает несколько секунд — делаем в фоне, чтобы кнопка отвечала сразу."""
    account, uid, folder = target
    try:
        await asyncio.to_thread(_imap_set_seen, account, uid, folder, seen)
    except Exception as e:
        logger.warning("IMAP set_seen failed: %s", e)
        await services.mark_read_local(message_id, user, not seen)
        await _refresh_markup(message, message_id, user)
        await message.answer(f"⚠️ Не удалось отметить письмо на почтовом сервере: {esc(e)}")
    finally:
        _seen_in_progress.discard(message_id)


@router.callback_query(F.data.startswith("m:seen:"))
async def toggle_seen(callback: CallbackQuery, user: TelegramUser):
    message_id = _message_id(callback)
    if message_id in _seen_in_progress:
        return await callback.answer("⏳ Уже обновляю…")
    seen = not await services.is_read(message_id, user)
    # Отвечаем Telegram сразу (иначе «query is too old»), кнопку меняем оптимистично
    await callback.answer("✅ Отмечено прочитанным" if seen else "✉️ Отмечено непрочитанным")
    target = await services.mark_read_local(message_id, user, seen)
    await _refresh_markup(callback.message, message_id, user)
    if target:
        _seen_in_progress.add(message_id)
        asyncio.create_task(_sync_seen_to_server(callback.message, message_id, user, target, seen))
