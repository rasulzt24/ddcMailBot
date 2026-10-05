"""Справочник: подсказки адресатов при вводе (inline-режим) и карточка выбранного контакта.

Кнопка «🔎 Найти в справочнике» подставляет в поле ввода «@бот » — по мере набора Telegram
показывает список людей. Выбранный человек приходит в чат текстом «"Имя" <email>»:
  * при вводе получателей (Кому / Копия) его подхватывает compose.got_recipients;
  * в остальное время бот показывает карточку контакта с кнопкой «✉️ Написать».
"""
import hashlib
from email.utils import getaddresses

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
                           Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from apps.accounts.models import TelegramUser
from apps.mail.services.directory import Person

from .. import services
from ..render import esc

router = Router()

MAX_RESULTS = 20


def _result(p: Person) -> InlineQueryResultArticle:
    who = "🕘 " if p.source == "history" else ""
    description = " · ".join(x for x in (p.email, p.details) if x)
    return InlineQueryResultArticle(
        id=hashlib.sha1(p.email.encode()).hexdigest(),
        title=f"{who}{p.name or p.email}",
        description=description[:200],
        # parse_mode=None: у бота по умолчанию HTML, и «<email>» был бы принят за тег
        input_message_content=InputTextMessageContent(message_text=services.person_address(p), parse_mode=None),
    )


@router.inline_query()
async def inline_search(query: InlineQuery, user: TelegramUser):
    text = query.query.strip()
    if len(text) < 2:
        # Пока ничего не набрано — частые адресаты
        people = [Person(name=c.name, email=c.email, source="history")
                  for c in await services.frequent_contacts(user, [])]
        await query.answer([_result(p) for p in people], cache_time=5, is_personal=True)
        return
    people = await services.find_people(user, text, limit=MAX_RESULTS)
    await query.answer([_result(p) for p in people], cache_time=30, is_personal=True)


def _single_address(text: str) -> tuple[str, str] | None:
    pairs = [(n, e) for n, e in getaddresses([text]) if "@" in e]
    return (pairs[0][0], pairs[0][1].lower()) if len(pairs) == 1 else None


@router.message(StateFilter(None), F.via_bot, F.text)
async def picked_outside_compose(message: Message, bot: Bot, user: TelegramUser, state: FSMContext):
    """Контакт выбран из справочника не во время составления письма — показываем карточку."""
    if message.via_bot.id != bot.id:
        return
    parsed = _single_address(message.text)
    if not parsed:
        return
    name, email = parsed
    people = await services.find_people(user, email, limit=5)
    p = next((x for x in people if x.email == email), Person(name=name, email=email))
    lines = [f"👤 <b>{esc(p.name or p.email)}</b>", f"📧 <code>{esc(p.email)}</code>"]
    if p.title:
        lines.append(f"💼 {esc(p.title)}")
    if p.department:
        lines.append(f"🏢 {esc(p.department)}")
    if p.phone:
        lines.append(f"☎️ {esc(p.phone)}")
    await state.update_data(contact_card=services.person_address(p))
    kb = InlineKeyboardBuilder()
    kb.button(text="✉️ Написать письмо", callback_data="dir:write")
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data == "dir:write")
async def write_to_contact(callback: CallbackQuery, state: FSMContext, user: TelegramUser):
    from .compose import start_new_to

    address = (await state.get_data()).get("contact_card")
    await callback.answer()
    if not address:
        return await callback.message.answer("Контакт устарел — найдите его снова.")
    await start_new_to(callback.message, state, user, address)
