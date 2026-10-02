import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, ErrorEvent, MenuButtonCommands
from django.conf import settings

from .handlers import admin, compose, fallback, mailbox, messages, settings as settings_handlers, start
from .middlewares import AuthMiddleware
from .notifier import Notifier

logger = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="inbox", description="Входящие"),
    BotCommand(command="new", description="Написать письмо"),
    BotCommand(command="search", description="Поиск по письмам"),
    BotCommand(command="sent", description="Отправленные"),
    BotCommand(command="mailbox", description="Мой почтовый ящик"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="password", description="Сменить пароль от почты"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
    BotCommand(command="help", description="Помощь"),
]


def make_storage() -> BaseStorage:
    if settings.REDIS_URL:
        from aiogram.fsm.storage.redis import RedisStorage
        from redis.asyncio import Redis
        from redis.backoff import ExponentialBackoff
        from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError
        from redis.asyncio.retry import Retry

        # Redis удалённый, связь иногда «подвисает» — повторяем запросы и держим соединение живым
        redis = Redis.from_url(
            settings.REDIS_URL,
            socket_timeout=10,
            socket_connect_timeout=10,
            socket_keepalive=True,
            health_check_interval=30,
            retry=Retry(ExponentialBackoff(cap=3, base=0.3), retries=3),
            retry_on_error=[RedisConnectionError, RedisTimeoutError],
        )
        return RedisStorage(redis=redis)
    logger.warning("REDIS_URL не задан — FSM в памяти, черновики пропадут при перезапуске")
    return MemoryStorage()


async def on_error(event: ErrorEvent):
    """Пользователь не должен «висеть» без ответа, если упала БД/Redis/сеть."""
    if "query is too old" in str(event.exception):
        # Нажатие кнопки обработано позже 15 с — безвредно, просто «протухший» колбэк
        logger.info("Stale callback ignored")
        return
    logger.error("Update failed: %r", event.exception, exc_info=event.exception)
    update = event.update
    text = "⚠️ Временная ошибка связи с сервером. Повторите действие через пару секунд."
    try:
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.message:
            await update.message.answer(text)
    except Exception:
        pass


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=make_storage())
    dp.errors.register(on_error)
    dp.message.filter(F.chat.type == "private")
    dp.callback_query.filter(F.message.chat.type == "private")
    dp.message.outer_middleware(AuthMiddleware())
    dp.callback_query.outer_middleware(AuthMiddleware())
    dp.include_routers(
        admin.router,
        start.router,
        mailbox.router,
        settings_handlers.router,
        messages.router,
        compose.router,
        fallback.router,  # последним
    )
    return dp


async def set_commands(bot: Bot):
    await bot.set_my_commands(COMMANDS, BotCommandScopeDefault())
    # Кнопка «Меню» слева от поля ввода — список команд
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    try:
        await bot.set_my_commands(COMMANDS + [BotCommand(command="admin", description="Панель суперадмина")],
                                  BotCommandScopeChat(chat_id=settings.SUPERADMIN_TELEGRAM_ID))
    except Exception as e:
        logger.warning("Can't set superadmin commands (напишите боту /start): %s", e)


async def run():
    bot = Bot(settings.TELEGRAM_BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = build_dispatcher()
    await set_commands(bot)
    notifier_task = asyncio.create_task(Notifier(bot).run())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        notifier_task.cancel()
        await bot.session.close()
