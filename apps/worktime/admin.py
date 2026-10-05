from django.contrib import admin

from .models import WorkdayLog, WorkdayProfile


@admin.register(WorkdayProfile)
class WorkdayProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "login", "bitrix_user_id", "remind_start", "remind_end", "auth_failed_at")
    readonly_fields = ("password_encrypted", "created_at", "updated_at")


@admin.register(WorkdayLog)
class WorkdayLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "requested_time", "ok", "error")
    list_filter = ("action", "ok")
