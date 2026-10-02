"""Удаляем файлы с диска вместе с записями (FileField сам этого не делает)."""
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import MailAttachment, MailMessage, OutgoingAttachment


@receiver(post_delete, sender=MailAttachment)
@receiver(post_delete, sender=OutgoingAttachment)
def delete_attachment_file(sender, instance, **kwargs):
    if instance.file:
        instance.file.delete(save=False)


@receiver(post_delete, sender=MailMessage)
def delete_raw_file(sender, instance, **kwargs):
    if instance.raw:
        instance.raw.delete(save=False)
