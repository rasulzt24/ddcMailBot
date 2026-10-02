from django.contrib import admin

from .models import TelegramUser


@admin.register(TelegramUser)
class TelegramUserAdmin(admin.ModelAdmin):
    list_display = ("telegram_id", "full_name", "username", "status", "is_superadmin", "last_seen_at", "created_at")
    list_filter = ("status", "is_superadmin", "notifications_enabled")
    search_fields = ("telegram_id", "full_name", "username")
    readonly_fields = ("created_at", "updated_at", "last_seen_at")
    actions = ("approve", "block")

    @admin.action(description="Подтвердить доступ")
    def approve(self, request, queryset):
        queryset.update(status=TelegramUser.Status.ACTIVE)

    @admin.action(description="Заблокировать")
    def block(self, request, queryset):
        queryset.filter(is_superadmin=False).update(status=TelegramUser.Status.BLOCKED)
