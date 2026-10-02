"""Отправка карточек и файлов в Telegram с учётом лимитов."""
import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, FSInputFile, ReplyParameters

from apps.accounts.models import TelegramUser

from . import services
from .render import esc, fmt_size

logger = logging.getLogger(__name__)

TG_UPLOAD_LIMIT = 50 * 1024 * 1024


async def retry(call, *args, **kwargs):
    for _ in range(5):
        try:
            return await call(*args, **kwargs)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    return await call(*args, **kwargs)


def _reply(reply_to: int | None):
    return ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None


async def send_attachments(bot: Bot, chat_id: int, attachments: list[services.AttachmentInfo],
                           reply_to: int | None = None) -> None:
    for a in attachments:
        if a.size > TG_UPLOAD_LIMIT:
            await retry(bot.send_message, chat_id,
                        f"📎 <b>{esc(a.filename)}</b> ({fmt_size(a.size)}) — больше лимита Telegram 50 МБ.",
                        reply_parameters=_reply(reply_to))
            continue
        sent = None
        if a.tg_file_id:
            try:
                sent = await retry(bot.send_document, chat_id, a.tg_file_id, reply_parameters=_reply(reply_to))
            except TelegramBadRequest:
                sent = None
        if sent is None:
            try:
                if a.path:
                    document = FSInputFile(a.path, filename=a.filename)
                else:
                    document = BufferedInputFile(await services.read_attachment(a.id), filename=a.filename)
                sent = await retry(bot.send_document, chat_id, document, reply_parameters=_reply(reply_to))
            except Exception as e:
                logger.warning("Can't send attachment %s: %s", a.id, e)
                await retry(bot.send_message, chat_id, f"⚠️ Не удалось отправить «{esc(a.filename)}»: {esc(e)}")
                continue
            doc = sent.document
            if doc:
                await services.cache_file_id(a.id, doc.file_id)
        await asyncio.sleep(0.3)


async def send_card(bot: Bot, chat_id: int, user: TelegramUser, card: services.Card,
                    with_attachments: bool, reply_to: int | None = None) -> int:
    sent = await retry(bot.send_message, chat_id, card.text, reply_markup=card.markup,
                       reply_parameters=_reply(reply_to))
    await services.record_notification(card.message_id, user, chat_id, sent.message_id)
    if with_attachments and card.attachments:
        await send_attachments(bot, chat_id, card.attachments, reply_to=sent.message_id)
    return sent.message_id


async def send_long_text(bot: Bot, chat_id: int, title: str, text: str, filename: str, reply_markup=None) -> None:
    """Длинный текст — несколькими сообщениями, очень длинный — файлом."""
    chunk = 3800
    if len(text) <= chunk * 3:
        parts = [text[i:i + chunk] for i in range(0, len(text), chunk)] or ["—"]
        for i, part in enumerate(parts):
            head = f"{title}\n\n" if i == 0 else ""
            markup = reply_markup if i == len(parts) - 1 else None
            await retry(bot.send_message, chat_id, head + esc(part), reply_markup=markup)
    else:
        await retry(bot.send_document, chat_id, BufferedInputFile(text.encode("utf-8"), filename=filename),
                    caption=title, reply_markup=reply_markup)
