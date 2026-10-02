"""Лёгкие события между процессами через Redis pub/sub.

Источник правды — всегда БД; событие лишь «будит» бота/воркер, чтобы не ждать
следующего цикла опроса. Без Redis всё продолжает работать через опрос.
"""
import json
import logging
from functools import lru_cache

from django.conf import settings

logger = logging.getLogger(__name__)

BOT_CHANNEL = "mailbot:bot"        # воркер -> бот: новые письма, результаты отправки, ошибки
WORKER_CHANNEL = "mailbot:worker"  # бот -> воркер: новое исходящее, подключён ящик


@lru_cache(maxsize=1)
def _client():
    import redis
    return redis.from_url(settings.REDIS_URL, decode_responses=True, health_check_interval=30, socket_keepalive=True)


def publish(channel: str, event: str, **payload) -> None:
    if not settings.REDIS_URL:
        return
    try:
        _client().publish(channel, json.dumps({"event": event, **payload}))
    except Exception as e:
        logger.warning("Redis publish failed: %s", e)


def notify_bot(event: str, **payload) -> None:
    publish(BOT_CHANNEL, event, **payload)


def notify_worker(event: str, **payload) -> None:
    publish(WORKER_CHANNEL, event, **payload)
