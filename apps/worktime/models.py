from django.db import models

from apps.accounts.models import TelegramUser
from apps.mail import crypto


class WorkdayProfile(models.Model):
    """Учёт рабочего времени в Битриксе для пользователя бота."""

    user = models.OneToOneField(TelegramUser, on_delete=models.CASCADE, related_name="workday",
                                verbose_name="Пользователь")
    # Пусто — берутся логин и пароль от почты (в Битриксе вход через тот же домен)
    login = models.CharField("Логин Битрикс", max_length=255, blank=True)
    password_encrypted = models.TextField("Пароль Битрикс (зашифрован)", blank=True)
    bitrix_user_id = models.PositiveIntegerField("ID в Битриксе", null=True, blank=True)

    remind_start = models.TimeField("Напомнить начать день", null=True, blank=True)
    remind_end = models.TimeField("Напомнить завершить день", null=True, blank=True)
    last_start_reminder = models.DateField(null=True, blank=True)
    last_end_reminder = models.DateField(null=True, blank=True)

    auth_failed_at = models.DateTimeField("Последняя ошибка входа", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Рабочий день: профиль"
        verbose_name_plural = "Рабочий день: профили"

    def __str__(self):
        return str(self.user)

    @property
    def password(self) -> str:
        return crypto.decrypt(self.password_encrypted) if self.password_encrypted else ""

    @password.setter
    def password(self, value: str):
        self.password_encrypted = crypto.encrypt(value) if value else ""


class WorkdayLog(models.Model):
    """Журнал действий: кто, когда и с каким временем открыл/закрыл день."""

    class Action(models.TextChoices):
        OPEN = "open", "Начало дня"
        CLOSE = "close", "Завершение дня"

    user = models.ForeignKey(TelegramUser, on_delete=models.CASCADE, related_name="workday_logs")
    action = models.CharField(max_length=8, choices=Action.choices)
    requested_time = models.TimeField("Указанное время")
    ok = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Рабочий день: действие"
        verbose_name_plural = "Рабочий день: журнал"
        ordering = ["-created_at"]
