import uuid

from django.conf import settings
from django.db import models

from apps.accounts.models import TelegramUser

from . import crypto


def _imap_host():
    return settings.MAIL_IMAP_HOST


def _imap_port():
    return settings.MAIL_IMAP_PORT


def _smtp_host():
    return settings.MAIL_SMTP_HOST


def _smtp_port():
    return settings.MAIL_SMTP_PORT


def _smtp_security():
    return settings.MAIL_SMTP_SECURITY


class MailAccount(models.Model):
    """Почтовый ящик, подключённый пользователем бота."""

    class SmtpSecurity(models.TextChoices):
        STARTTLS = "starttls", "STARTTLS"
        SSL = "ssl", "SSL/TLS"
        NONE = "none", "Без шифрования"

    owner = models.ForeignKey(TelegramUser, on_delete=models.CASCADE, related_name="mail_accounts", verbose_name="Владелец")
    email = models.EmailField("E-mail")
    display_name = models.CharField("Имя отправителя", max_length=255, blank=True)
    login = models.CharField("Логин", max_length=255)
    password_encrypted = models.TextField("Пароль (зашифрован)")

    imap_host = models.CharField("IMAP хост", max_length=255, default=_imap_host)
    imap_port = models.PositiveIntegerField("IMAP порт", default=_imap_port)
    smtp_host = models.CharField("SMTP хост", max_length=255, default=_smtp_host)
    smtp_port = models.PositiveIntegerField("SMTP порт", default=_smtp_port)
    smtp_security = models.CharField("SMTP шифрование", max_length=16, choices=SmtpSecurity.choices,
                                     default=_smtp_security)
    sent_folder = models.CharField("Папка «Отправленные»", max_length=255, blank=True)

    is_active = models.BooleanField("Синхронизация включена", default=True)
    needs_reauth = models.BooleanField("Требуется повторный вход", default=False)
    reauth_notified_at = models.DateTimeField("Напоминание о пароле отправлено", null=True, blank=True)

    inbox_uidvalidity = models.PositiveBigIntegerField("UIDVALIDITY", null=True, blank=True)
    last_uid = models.PositiveBigIntegerField("Последний UID", default=0)
    sent_uidvalidity = models.PositiveBigIntegerField("UIDVALIDITY «Отправленных»", null=True, blank=True)
    sent_last_uid = models.PositiveBigIntegerField("Последний UID «Отправленных»", default=0)
    last_sync_at = models.DateTimeField("Последняя синхронизация", null=True, blank=True)
    last_error = models.TextField("Последняя ошибка", blank=True)
    error_count = models.PositiveIntegerField("Ошибок подряд", default=0)

    created_at = models.DateTimeField("Подключён", auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Почтовый ящик"
        verbose_name_plural = "Почтовые ящики"
        constraints = [models.UniqueConstraint(fields=["owner", "email"], name="uniq_owner_email")]

    def __str__(self):
        return self.email

    @property
    def password(self) -> str:
        return crypto.decrypt(self.password_encrypted)

    @password.setter
    def password(self, value: str):
        self.password_encrypted = crypto.encrypt(value)

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1]


class Contact(models.Model):
    """Адресная книга: наполняется автоматически из входящих и отправленных писем."""

    account = models.ForeignKey(MailAccount, on_delete=models.CASCADE, related_name="contacts")
    email = models.EmailField("E-mail")
    name = models.CharField("Имя", max_length=255, blank=True)
    seen_count = models.PositiveIntegerField("Встречался в письмах", default=0)
    sent_count = models.PositiveIntegerField("Писали ему", default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Контакт"
        verbose_name_plural = "Контакты"
        constraints = [models.UniqueConstraint(fields=["account", "email"], name="uniq_contact_email")]
        indexes = [models.Index(fields=["account", "-sent_count", "-seen_count"])]

    def __str__(self):
        return f"{self.name} <{self.email}>" if self.name else self.email


class MailThread(models.Model):
    """Цепочка переписки (conversation)."""

    account = models.ForeignKey(MailAccount, on_delete=models.CASCADE, related_name="threads")
    subject = models.CharField("Тема (нормализованная)", max_length=500, blank=True, db_index=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Цепочка"
        verbose_name_plural = "Цепочки"
        ordering = ["-last_message_at"]

    def __str__(self):
        return self.subject or f"Цепочка #{self.pk}"


def raw_upload_to(instance, filename):
    return f"raw/{instance.account_id}/{uuid.uuid4().hex}.eml"


class MailMessage(models.Model):
    class Direction(models.TextChoices):
        INCOMING = "in", "Входящее"
        OUTGOING = "out", "Исходящее"

    class Kind(models.TextChoices):
        NEW = "new", "Новое"
        REPLY = "reply", "Ответ"
        FORWARD = "forward", "Пересланное"
        AUTO_REPLY = "auto", "Автоответ"
        CALENDAR = "calendar", "Приглашение"

    class Importance(models.TextChoices):
        LOW = "low", "Низкая"
        NORMAL = "normal", "Обычная"
        HIGH = "high", "Высокая"

    account = models.ForeignKey(MailAccount, on_delete=models.CASCADE, related_name="messages")
    thread = models.ForeignKey(MailThread, on_delete=models.SET_NULL, null=True, blank=True, related_name="messages")
    direction = models.CharField(max_length=3, choices=Direction.choices, default=Direction.INCOMING)
    folder = models.CharField("Папка", max_length=255, default="INBOX")
    uid = models.PositiveBigIntegerField(null=True, blank=True)
    uidvalidity = models.PositiveBigIntegerField(null=True, blank=True)

    message_id = models.CharField("Message-ID", max_length=512, db_index=True)
    in_reply_to = models.CharField("In-Reply-To", max_length=512, blank=True)
    references = models.TextField("References", blank=True)

    kind = models.CharField("Тип", max_length=16, choices=Kind.choices, default=Kind.NEW)
    importance = models.CharField("Важность", max_length=8, choices=Importance.choices, default=Importance.NORMAL)
    from_name = models.CharField("От (имя)", max_length=255, blank=True)
    from_email = models.CharField("От (e-mail)", max_length=320, blank=True, db_index=True)
    reply_to = models.CharField("Reply-To", max_length=512, blank=True)
    subject = models.TextField("Тема", blank=True)
    date = models.DateTimeField("Дата", db_index=True)

    body_text = models.TextField("Текст", blank=True)
    body_html = models.TextField("HTML", blank=True)
    body_new_text = models.TextField("Текст без цитаты", blank=True)

    is_read = models.BooleanField("Прочитано", default=False)
    # Когда пользователь сменил статус в боте: пока изменение не дошло до сервера,
    # синхронизация флагов не должна откатить его обратно
    read_changed_at = models.DateTimeField("Статус прочтения изменён в боте", null=True, blank=True)
    size = models.PositiveIntegerField("Размер", default=0)
    raw = models.FileField("Оригинал (.eml)", upload_to=raw_upload_to, null=True, blank=True)

    notify = models.BooleanField("Уведомлять", default=True)
    notified_at = models.DateTimeField("Уведомление отправлено", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Письмо"
        verbose_name_plural = "Письма"
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "folder", "uidvalidity", "uid"],
                condition=models.Q(uid__isnull=False),
                name="uniq_message_uid",
            )
        ]
        indexes = [
            models.Index(fields=["account", "direction", "-date"]),
            models.Index(fields=["notified_at", "notify"]),
        ]

    def __str__(self):
        return self.subject or "(без темы)"

    @property
    def from_display(self) -> str:
        if self.from_name and self.from_email:
            return f"{self.from_name} <{self.from_email}>"
        return self.from_name or self.from_email


class MessageRecipient(models.Model):
    class Kind(models.TextChoices):
        TO = "to", "Кому"
        CC = "cc", "Копия"
        BCC = "bcc", "Скрытая копия"

    message = models.ForeignKey(MailMessage, on_delete=models.CASCADE, related_name="recipients")
    kind = models.CharField(max_length=3, choices=Kind.choices)
    name = models.CharField(max_length=255, blank=True)
    email = models.CharField(max_length=320)

    class Meta:
        verbose_name = "Получатель"
        verbose_name_plural = "Получатели"

    def __str__(self):
        return f"{self.name} <{self.email}>" if self.name else self.email


def attachment_upload_to(instance, filename):
    return f"attachments/{instance.message.account_id}/{instance.message_id}/{filename}"


class MailAttachment(models.Model):
    message = models.ForeignKey(MailMessage, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=attachment_upload_to, max_length=500)
    filename = models.CharField("Имя файла", max_length=255)
    content_type = models.CharField(max_length=255, blank=True)
    size = models.PositiveIntegerField(default=0)
    content_id = models.CharField("Content-ID", max_length=255, blank=True)
    is_inline = models.BooleanField("Встроено в текст", default=False)
    tg_file_id = models.CharField("Telegram file_id (кэш)", max_length=255, blank=True)

    class Meta:
        verbose_name = "Вложение"
        verbose_name_plural = "Вложения"

    def __str__(self):
        return self.filename


class TelegramNotification(models.Model):
    """Связь письма с сообщением в Telegram — чтобы отвечать на письмо реплаем."""

    message = models.ForeignKey(MailMessage, on_delete=models.CASCADE, related_name="notifications")
    user = models.ForeignKey(TelegramUser, on_delete=models.CASCADE, related_name="+")
    chat_id = models.BigIntegerField()
    tg_message_id = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["chat_id", "tg_message_id"])]


class OutgoingMessage(models.Model):
    class Mode(models.TextChoices):
        NEW = "new", "Новое"
        REPLY = "reply", "Ответ"
        REPLY_ALL = "reply_all", "Ответ всем"
        FORWARD = "forward", "Пересылка"

    class Status(models.TextChoices):
        QUEUED = "queued", "В очереди"
        SENDING = "sending", "Отправляется"
        SENT = "sent", "Отправлено"
        FAILED = "failed", "Ошибка"

    account = models.ForeignKey(MailAccount, on_delete=models.CASCADE, related_name="outgoing")
    created_by = models.ForeignKey(TelegramUser, on_delete=models.CASCADE, related_name="outgoing")
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.NEW)
    source_message = models.ForeignKey(MailMessage, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="+", verbose_name="Исходное письмо")

    to = models.JSONField("Кому", default=list)
    cc = models.JSONField("Копия", default=list, blank=True)
    bcc = models.JSONField("Скрытая копия", default=list, blank=True)
    subject = models.TextField("Тема", blank=True)
    body_text = models.TextField("Текст", blank=True)
    body_html = models.TextField("HTML", blank=True)
    forward_attachments = models.ManyToManyField(MailAttachment, blank=True, related_name="+",
                                                 verbose_name="Вложения исходного письма")

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    smtp_message_id = models.CharField(max_length=512, blank=True)
    sent_message = models.OneToOneField(MailMessage, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    result_notified = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Исходящее письмо"
        verbose_name_plural = "Исходящие письма"
        ordering = ["-created_at"]

    def __str__(self):
        return self.subject or "(без темы)"


def outgoing_upload_to(instance, filename):
    return f"outgoing/{instance.outgoing.account_id}/{instance.outgoing_id}/{filename}"


class OutgoingAttachment(models.Model):
    outgoing = models.ForeignKey(OutgoingMessage, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=outgoing_upload_to, max_length=500)
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=255, blank=True)
    size = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Файл исходящего письма"
        verbose_name_plural = "Файлы исходящих писем"

    def __str__(self):
        return self.filename
