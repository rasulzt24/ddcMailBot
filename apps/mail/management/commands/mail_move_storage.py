"""Переносит уже сохранённые файлы с локального диска (MEDIA_ROOT) в текущее хранилище (MinIO/S3).

    python manage.py mail_move_storage            # скопировать
    python manage.py mail_move_storage --delete   # скопировать и удалить локальные копии

Имена файлов не меняются, поэтому записи в БД править не нужно. Повторный запуск безопасен.
"""
from django.conf import settings
from django.core.files.storage import FileSystemStorage, default_storage
from django.core.management.base import BaseCommand, CommandError

from apps.mail.models import MailAttachment, MailMessage, OutgoingAttachment


class Command(BaseCommand):
    help = "Перенести файлы с локального диска в MinIO/S3"

    def add_arguments(self, parser):
        parser.add_argument("--delete", action="store_true", help="удалить локальные копии после переноса")

    def handle(self, *args, delete, **options):
        if not settings.S3_ENDPOINT_URL:
            raise CommandError("S3_ENDPOINT_URL не задан — хранилище по умолчанию и так локальное")
        local = FileSystemStorage(location=settings.MEDIA_ROOT)
        names = set()
        names |= set(MailAttachment.objects.exclude(file="").values_list("file", flat=True))
        names |= set(OutgoingAttachment.objects.exclude(file="").values_list("file", flat=True))
        names |= set(MailMessage.objects.exclude(raw="").exclude(raw__isnull=True).values_list("raw", flat=True))

        copied = skipped = missing = 0
        for name in sorted(names):
            if not local.exists(name):
                missing += 1
                continue
            if default_storage.exists(name):
                skipped += 1
            else:
                with local.open(name, "rb") as f:
                    saved = default_storage.save(name, f)
                if saved != name:
                    raise CommandError(f"Хранилище переименовало {name} -> {saved}; проверьте file_overwrite")
                copied += 1
            if delete:
                local.delete(name)
        self.stdout.write(self.style.SUCCESS(
            f"Перенесено: {copied}, уже были: {skipped}, нет на диске: {missing}, всего записей: {len(names)}"))
