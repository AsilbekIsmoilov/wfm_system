from django.contrib import admin
from .models import AttendanceLog


@admin.register(AttendanceLog)
class AttendanceLogAdmin(admin.ModelAdmin):
    list_display = (
        "operator",
        "date",
        "scheduled_start",
        "login_time",
        "status",
        "delay_minutes",
        "comment",
        "comment_created_by",
        "comment_updated_by",
        "comment_updated_at",
    )

    list_filter = (
        "status",
        "date",
    )

    search_fields = (
        "operator__surname",
        "operator__name",
        "operator__middle_name",
        "operator__login_id",
    )

    ordering = ("-date", "scheduled_start")

    readonly_fields = (
        "operator",
        "date",
        "scheduled_start",
        "login_time",
        "status",
        "delay_minutes",
    )