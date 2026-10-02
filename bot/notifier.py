"""Фоновая задача бота: доставка уведомлений о новых письмах и результатах отправки."""
import asyncio
import json
import logging

from aiogram import Bot
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
                                TelegramServerError)
from django.conf import settings

from apps.mail.services import events

from . import services
from .render import esc
from .keyboards import reauth_kb
from .sending import retry, send_card

logger = logging.getLogger(__name__)

POLL_INTERVAL = 15  # страховочный опрос БД, даже если Redis-событие потерялось


class Notifier:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.wakeup = asyncio.Event()

    async def run(self):
        tasks = [asyncio.create_task(self._loop())]
        if settings.REDIS_URL:
            tasks.append(asyncio.create_task(self._listen()))
        await asyncio.gather(*tasks)

    async def _listen(self):
        import redis.asyncio as aioredis
        while True:
            try:
                client = aioredis.from_url(settings.REDIS_URL, decode_responses=True, health_check_interval=30,
                                          socket_keepalive=True)
                async with client.pubsub() as pubsub:
                    await pubsub.subscribe(events.BOT_CHANNEL)
                    logger.info("Subscribed to Redis channel %s", events.BOT_CHANNEL)
                    while True:
                        # get_message с таймаутом (а не listen) — переживает «тихие» обрывы и шлёт health-check
                        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=10)
                        if msg:
                            self._on_event(msg.get("data"))
                            self.wakeup.set()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Redis listener error: %s", e)
                await asyncio.sleep(5)

    def _on_event(self, data) -> None:
        try:
            event = json.loads(data) if isinstance(data, str) else {}
        except ValueError:
            return
        if event.get("event") == "read_changed" and event.get("message_ids"):
            asyncio.create_task(self.refresh_cards(event["message_ids"]))

    async def refresh_cards(self, message_ids: list[int]) -> None:
        """Письмо прочитали в Outlook — переключаем кнопку «Прочитано» на уже отправленных карточках."""
        try:
            for message_id, user, chat_id, tg_message_id in await services.cards_to_refresh(message_ids):
                card = await services.card_for(message_id, user)
                try:
                    await retry(self.bot.edit_message_reply_markup, chat_id=chat_id, message_id=tg_message_id,
                                reply_markup=card.markup)
                except TelegramBadRequest:
                    pass  # «message is not modified» / карточка удалена или слишком старая
                await asyncio.sleep(0.1)
        except Exception as e:
            logger.warning("Can't refresh cards %s: %s", message_ids, e)

    async def _loop(self):
        while True:
            try:
                await services.close_db()
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Notifier flush failed")
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
            self.wakeup.clear()

    async def flush(self):
        for message_id in await services.pending_notifications():
            try:
                user, card = await services.notification_card(message_id)
                await send_card(self.bot, user.telegram_id, user, card, with_attachments=card.auto_send_attachments)
            except TelegramForbiddenError:
                logger.info("User blocked the bot, skip message %s", message_id)
            except (TelegramNetworkError, TelegramServerError) as e:
                # Telegram недоступен (сеть / прокси / VPN) — не теряем уведомление, повторим в следующем цикле.
                # Если карточка уже ушла, а упали только вложения — повторно карточку не шлём.
                if not await services.card_delivered(message_id):
                    logger.warning("Telegram unavailable, message %s will be retried: %s", message_id, e)
                    return
                logger.warning("Message %s: card sent, attachments failed: %s", message_id, e)
            except Exception:
                logger.exception("Can't deliver message %s", message_id)
            await services.mark_notified(message_id)
            await asyncio.sleep(0.5)

        for chat_id, text in await services.pending_outgoing_results():
            await self._send(chat_id, text)

        for chat_id, email, is_reminder in await services.pending_reauth():
            head = "⏰ <b>Напоминание:</b> пароль" if is_reminder else "🔐 <b>Пароль"
            await self._send(
                chat_id,
                f"{head} от почты {esc(email)} не подходит</b>\n\n"
                f"Скорее всего, вы сменили пароль от учётной записи. Проверка почты и отправка писем "
                f"приостановлены, чтобы учётную запись не заблокировали за неверные попытки входа.\n\n"
                f"Нажмите кнопку и отправьте новый пароль:",
                reply_markup=reauth_kb(),
            )

    async def _send(self, chat_id: int, text: str, reply_markup=None):
        try:
            await retry(self.bot.send_message, chat_id, text, reply_markup=reply_markup)
        except Exception as e:
            logger.warning("Can't send to %s: %s", chat_id, e)
