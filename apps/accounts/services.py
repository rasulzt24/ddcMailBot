from django.conf import settings
from django.utils import timezone

from .models import TelegramUser


def get_or_register(telegram_id: int, username: str = "", full_name: str = "") -> tuple[TelegramUser, bool]:
    """Возвращает пользователя бота, регистрируя его при первом обращении."""
    is_superadmin = telegram_id == settings.SUPERADMIN_TELEGRAM_ID
    user, created = TelegramUser.objects.get_or_create(
        telegram_id=telegram_id,
        defaults={
            "username": username or "",
            "full_name": full_name or "",
            "is_superadmin": is_superadmin,
            "last_seen_at": timezone.now(),
            "status": (
                TelegramUser.Status.ACTIVE
                if is_superadmin or not settings.REQUIRE_APPROVAL
                else TelegramUser.Status.PENDING
            ),
        },
    )

    changed = {"last_seen_at"}
    user.last_seen_at = timezone.now()
    if username and user.username != username:
        user.username = username
        changed.add("username")
    if full_name and user.full_name != full_name:
        user.full_name = full_name
        changed.add("full_name")
    if is_superadmin and not user.is_superadmin:
        user.is_superadmin = True
        user.status = TelegramUser.Status.ACTIVE
        changed |= {"is_superadmin", "status"}
    if not created:
        user.save(update_fields=list(changed))
    return user, created
