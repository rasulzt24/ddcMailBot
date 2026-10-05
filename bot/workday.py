"""Рабочий день в Битриксе: доступ к профилю и вызовы клиента из бота."""
import asyncio
from datetime import time

from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.accounts.models import TelegramUser
from apps.mail.models import MailAccount
from apps.worktime.models import WorkdayLog, WorkdayProfile
from apps.worktime.services import bitrix
from apps.worktime.services.bitrix import BitrixAuthError, DayState  # noqa: F401 (DayState — для хендлеров)


class NeedCredentials(Exception):
    """Нет логина/пароля или Битрикс их не принял — нужно спросить у пользователя."""


@sync_to_async
def profile(user: TelegramUser) -> WorkdayProfile:
    return WorkdayProfile.objects.get_or_create(user=user)[0]


@sync_to_async
def _credentials(user: TelegramUser) -> tuple[WorkdayProfile, str, str]:
    prof = WorkdayProfile.objects.get_or_create(user=user)[0]
    account = MailAccount.objects.filter(owner=user).order_by("created_at").first()
    login = prof.login or (account.login.split("@")[0].split("\\")[-1] if account else "")
    password = prof.password or (account.password if account else "")
    return prof, login, password


@sync_to_async
def _mark(profile_id: int, *, failed: bool = False, bitrix_user_id: int | None = None) -> None:
    fields = {"auth_failed_at": timezone.now() if failed else None}
    if bitrix_user_id:
        fields["bitrix_user_id"] = bitrix_user_id
    WorkdayProfile.objects.filter(pk=profile_id).update(**fields)


@sync_to_async
def save_credentials(user: TelegramUser, login: str, password: str) -> None:
    prof = WorkdayProfile.objects.get_or_create(user=user)[0]
    bitrix.forget(prof.login or login)
    prof.login, prof.password, prof.auth_failed_at = login, password, None
    prof.save()


@sync_to_async
def save_reminder(user: TelegramUser, which: str, value: time | None) -> WorkdayProfile:
    prof = WorkdayProfile.objects.get_or_create(user=user)[0]
    setattr(prof, "remind_start" if which == "start" else "remind_end", value)
    prof.save()
    return prof


@sync_to_async
def log(user: TelegramUser, action: str, at: time, ok: bool, error: str = "") -> None:
    WorkdayLog.objects.create(user=user, action=action, requested_time=at, ok=ok, error=error[:1000])


async def call(user: TelegramUser, method: str, *args) -> DayState:
    """status() / open(time, record_id) / close(time, record_id). Сетевой запрос — в отдельном потоке."""
    prof, login, password = await _credentials(user)
    # После отказа входа не пробуем сами — иначе можно заблокировать учётку в AD
    if not login or not password or prof.auth_failed_at:
        raise NeedCredentials()
    client = bitrix.BitrixClient(login, password)
    try:
        result = await asyncio.to_thread(getattr(client, method), *args)
    except BitrixAuthError:
        await _mark(prof.pk, failed=True)
        raise NeedCredentials()
    user_id = await asyncio.to_thread(lambda: client.user_id)
    if user_id != prof.bitrix_user_id:
        await _mark(prof.pk, bitrix_user_id=user_id)
    return result


@sync_to_async
def reminder_candidates(kind: str) -> list[tuple[WorkdayProfile, TelegramUser]]:
    """Профили, которым пора напомнить сегодня (по будням, в окне после выбранного времени)."""
    now = timezone.localtime()
    if now.weekday() >= 5:
        return []
    field, last, window = ("remind_start", "last_start_reminder", 3) if kind == "start" else \
        ("remind_end", "last_end_reminder", 5)
    result = []
    qs = (WorkdayProfile.objects.select_related("user").exclude(**{f"{field}__isnull": True})
          .filter(user__notifications_enabled=True, auth_failed_at__isnull=True))
    for prof in qs:
        at = getattr(prof, field)
        minutes = (now.hour * 60 + now.minute) - (at.hour * 60 + at.minute)
        if getattr(prof, last) != now.date() and 0 <= minutes < window * 60 and prof.user.has_access:
            result.append((prof, prof.user))
    return result


@sync_to_async
def mark_reminded(profile_id: int, kind: str) -> None:
    field = "last_start_reminder" if kind == "start" else "last_end_reminder"
    WorkdayProfile.objects.filter(pk=profile_id).update(**{field: timezone.localdate()})
