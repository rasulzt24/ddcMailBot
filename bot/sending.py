"""Отправка карточек и файлов в Telegram с учётом лимитов."""
import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (BufferedInputFile, FSInputFile, InputMediaDocument, InputMediaPhoto, Message,
                           ReplyParameters)

from apps.accounts.models import TelegramUser

from . import services
from .render import esc, fmt_size

logger = logging.getLogger(__name__)

TG_UPLOAD_LIMIT = 50 * 1024 * 1024
TG_PHOTO_LIMIT = 10 * 1024 * 1024
ALBUM_SIZE = 10  # максимум файлов в одном альбоме Telegram
PHOTO_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}


async def retry(call, *args, **kwargs):
    for _ in range(5):
        try:
            return await call(*args, **kwargs)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    return await call(*args, **kwargs)


def _reply(reply_to: int | None):
    return ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None


# ---------- Вложение -> файл для Telegram ----------

def _kind(a: services.AttachmentInfo) -> str:
    """photo — показывается картинкой с превью; doc — файлом."""
    if (a.content_type or "").lower() in PHOTO_TYPES and a.size <= TG_PHOTO_LIMIT:
        return "photo"
    return "doc"


def _cached_id(a: services.AttachmentInfo, kind: str) -> str | None:
    """В кэше file_id хранится с типом («photo:…» / «doc:…»): фото и документ в Telegram не взаимозаменяемы."""
    if not a.tg_file_id:
        return None
    cached_kind, sep, file_id = a.tg_file_id.partition(":")
    if not sep:  # старый формат — это был документ
        return a.tg_file_id if kind == "doc" else None
    return file_id if cached_kind == kind else None


async def _upload(a: services.AttachmentInfo):
    if a.path:
        return FSInputFile(a.path, filename=a.filename)
    return BufferedInputFile(await services.read_attachment(a.id), filename=a.filename)


async def _media(a: services.AttachmentInfo, kind: str, caption: str | None = None):
    source = _cached_id(a, kind) or await _upload(a)
    cls = InputMediaPhoto if kind == "photo" else InputMediaDocument
    return cls(media=source, caption=caption)


async def _remember(a: services.AttachmentInfo, kind: str, msg: Message) -> None:
    file_id = msg.photo[-1].file_id if (kind == "photo" and msg.photo) else (msg.document.file_id if msg.document else None)
    if file_id and _cached_id(a, kind) != file_id:
        await services.cache_file_id(a.id, f"{kind}:{file_id}")


async def _send_single(bot: Bot, chat_id: int, a: services.AttachmentInfo, kind: str, *, caption=None,
                       reply_markup=None, reply_to=None) -> Message:
    """Один файл; если фото не принято (размеры/формат) — повторяем документом."""
    last_error = None
    for attempt_kind in ([kind, "doc"] if kind == "photo" else ["doc"]):
        method = bot.send_photo if attempt_kind == "photo" else bot.send_document
        try:
            source = _cached_id(a, attempt_kind) or await _upload(a)
            msg = await retry(method, chat_id, source, caption=caption, reply_markup=reply_markup,
                              reply_parameters=_reply(reply_to))
            await _remember(a, attempt_kind, msg)
            return msg
        except TelegramBadRequest as e:
            last_error = e
            logger.info("send %s as %s failed: %s", a.filename, attempt_kind, e)
    raise last_error


async def send_attachments(bot: Bot, chat_id: int, attachments: list[services.AttachmentInfo],
                           reply_to: int | None = None) -> None:
    """Фото — альбомом с превью, остальные файлы — альбомом документов (по 10 в альбоме)."""
    too_big = [a for a in attachments if a.size > TG_UPLOAD_LIMIT]
    for a in too_big:
        await retry(bot.send_message, chat_id,
                    f"📎 <b>{esc(a.filename)}</b> ({fmt_size(a.size)}) — больше лимита Telegram 50 МБ.",
                    reply_parameters=_reply(reply_to))
    ok = [a for a in attachments if a.size <= TG_UPLOAD_LIMIT]
    groups = {"photo": [a for a in ok if _kind(a) == "photo"], "doc": [a for a in ok if _kind(a) == "doc"]}

    for kind, items in groups.items():
        for i in range(0, len(items), ALBUM_SIZE):
            chunk = items[i:i + ALBUM_SIZE]
            if len(chunk) > 1:
                try:
                    media = [await _media(a, kind) for a in chunk]
                    sent = await retry(bot.send_media_group, chat_id, media, reply_parameters=_reply(reply_to))
                    for a, msg in zip(chunk, sent):
                        await _remember(a, kind, msg)
                    await asyncio.sleep(0.5)
                    continue
                except Exception as e:
                    # Например, одна из картинок не подошла под требования Telegram — шлём по одному
                    logger.info("album of %s failed, sending one by one: %s", kind, e)
            for a in chunk:
                try:
                    await _send_single(bot, chat_id, a, kind, reply_to=reply_to)
                except Exception as e:
                    logger.warning("Can't send attachment %s: %s", a.id, e)
                    await retry(bot.send_message, chat_id, f"⚠️ Не удалось отправить «{esc(a.filename)}»: {esc(e)}")
                await asyncio.sleep(0.3)


async def send_card(bot: Bot, chat_id: int, user: TelegramUser, card: services.Card,
                    with_attachments: bool, reply_to: int | None = None) -> int:
    sent = None
    # Одно вложение — карточка становится подписью к нему: одно сообщение с файлом и кнопками
    if with_attachments and card.caption and len(card.attachments) == 1 \
            and card.attachments[0].size <= TG_UPLOAD_LIMIT:
        a = card.attachments[0]
        try:
            sent = await _send_single(bot, chat_id, a, _kind(a), caption=card.caption,
                                      reply_markup=card.markup, reply_to=reply_to)
        except Exception as e:
            logger.info("card with attachment failed, fallback to text card: %s", e)
            sent = None
        if sent:
            await services.record_notification(card.message_id, user, chat_id, sent.message_id)
            return sent.message_id

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
