from django.contrib import admin

from .models import (Contact, MailAccount, MailAttachment, MailMessage, MailThread, MessageRecipient,
                     OutgoingAttachment, OutgoingMessage)


@admin.register(MailAccount)
class MailAccountAdmin(admin.ModelAdmin):
    list_display = ("email", "owner", "is_active", "needs_reauth", "last_sync_at", "error_count", "last_uid")
    list_filter = ("is_active", "needs_reauth")
    search_fields = ("email", "login", "owner__full_name", "owner__telegram_id")
    readonly_fields = ("password_encrypted", "inbox_uidvalidity", "last_uid", "last_sync_at", "last_error",
                       "created_at", "updated_at")
    actions = ("resync",)

    @admin.action(description="Сбросить ошибки и включить синхронизацию")
    def resync(self, request, queryset):
        queryset.update(is_active=True, needs_reauth=False, reauth_notified_at=None, error_count=0, last_error="")


class RecipientInline(admin.TabularInline):
    model = MessageRecipient
    extra = 0


class AttachmentInline(admin.TabularInline):
    model = MailAttachment
    extra = 0
    fields = ("filename", "content_type", "size", "is_inline", "file")
    readonly_fields = fields


@admin.register(MailMessage)
class MailMessageAdmin(admin.ModelAdmin):
    list_display = ("subject", "from_email", "account", "direction", "kind", "date", "is_read", "notified_at")
    list_filter = ("direction", "kind", "importance", "is_read", "account")
    search_fields = ("subject", "from_email", "from_name", "message_id", "body_text")
    date_hierarchy = "date"
    raw_id_fields = ("thread",)
    inlines = (RecipientInline, AttachmentInline)
    readonly_fields = ("message_id", "in_reply_to", "references", "uid", "uidvalidity", "created_at")


@admin.register(MailThread)
class MailThreadAdmin(admin.ModelAdmin):
    list_display = ("subject", "account", "last_message_at")
    search_fields = ("subject",)


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("email", "name", "account", "seen_count", "sent_count", "last_used_at")
    search_fields = ("email", "name")


class OutgoingAttachmentInline(admin.TabularInline):
    model = OutgoingAttachment
    extra = 0


@admin.register(OutgoingMessage)
class OutgoingMessageAdmin(admin.ModelAdmin):
    list_display = ("subject", "account", "mode", "status", "attempts", "created_at", "sent_at")
    list_filter = ("status", "mode")
    search_fields = ("subject",)
    raw_id_fields = ("source_message", "sent_message")
    filter_horizontal = ("forward_attachments",)
    inlines = (OutgoingAttachmentInline,)
    actions = ("requeue",)

    @admin.action(description="Повторить отправку")
    def requeue(self, request, queryset):
        queryset.exclude(status=OutgoingMessage.Status.SENT).update(
            status=OutgoingMessage.Status.QUEUED, attempts=0, next_attempt_at=None, result_notified=False)
