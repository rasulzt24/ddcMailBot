"""Запуск Telegram-бота: python manage.py runbot"""
import asyncio

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Запуск Telegram-бота (aiogram, long polling)"

    def handle(self, *args, **options):
        from bot.main import run

        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            self.stdout.write("Бот остановлен")
