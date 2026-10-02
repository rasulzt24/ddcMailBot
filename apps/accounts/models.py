from django.db import models


class TelegramUser(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает подтверждения"
        ACTIVE = "active", "Активен"
        BLOCKED = "blocked", "Заблокирован"

    telegram_id = models.BigIntegerField("Telegram ID", unique=True)
    username = models.CharField("Username", max_length=64, blank=True)
    full_name = models.CharField("Имя в Telegram", max_length=255, blank=True)
    status = models.CharField("Статус", max_length=16, choices=Status.choices, default=Status.PENDING)
    is_superadmin = models.BooleanField("Суперадмин", default=False)

    # Настройки пользователя
    notifications_enabled = models.BooleanField("Уведомления о новых письмах", default=True)
    send_attachments = models.BooleanField("Сразу присылать вложения", default=True)
    signature = models.TextField("Подпись в письмах", blank=True)

    created_at = models.DateTimeField("Зарегистрирован", auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_seen_at = models.DateTimeField("Последняя активность", null=True, blank=True)

    class Meta:
        verbose_name = "Пользователь бота"
        verbose_name_plural = "Пользователи бота"
        ordering = ["-created_at"]

    def __str__(self):
        name = self.full_name or self.username or str(self.telegram_id)
        return f"{name} ({self.telegram_id})"

    @property
    def has_access(self) -> bool:
        return self.is_superadmin or self.status == self.Status.ACTIVE
