"""
Django admin для всех моделей hourly_locks.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html

from .models import (
    AutomationOverride,
    Compensation,
    CompensationDay,
    CompensationDebtLink,
    Cycle,
    EventLog,
    Group,
    ManualAdjustment,
    Note,
    Operator,
    OperatorScheduleDay,
    RequestActionLog,
    RequestTypeRule,
    RequestSubtypeRule,
    Shift,
    SystemPolicy,
    Transfer,
    User,
    WorkDebt,
    WorkDebtDetail,
    WorkLogDaily,
)


# =============================================================================
# User / Operator / Group
# =============================================================================

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = (
        "username", "email", "role", "operator",
        "is_active", "is_staff", "is_superuser",
    )
    list_filter = ("role", "is_staff", "is_superuser", "is_active")
    search_fields = ("username", "email", "first_name", "last_name")
    ordering = ("username",)

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Личные данные", {"fields": ("first_name", "last_name", "email", "operator")}),
        ("Роли и доступ", {"fields": (
            "role", "is_active", "is_staff", "is_superuser",
            "groups", "user_permissions",
        )}),
        ("Даты", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": (
                "username", "email", "password1", "password2",
                "role", "operator",
            ),
        }),
    )
    autocomplete_fields = ("operator",)


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "supervisor_display", "operators_count", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "supervisor__username")
    autocomplete_fields = ("supervisor",)
    readonly_fields = ("created_at", "updated_at")

    def supervisor_display(self, obj):
        if not obj.supervisor:
            return "—"
        sup = obj.supervisor
        op = getattr(sup, "operator", None)
        if op:
            return f"{op.surname} {op.name}"
        return sup.username
    supervisor_display.short_description = "Супервайзер"

    def operators_count(self, obj):
        return obj.operators.filter(is_active=True).count()
    operators_count.short_description = "Операторов"


@admin.register(Operator)
class OperatorAdmin(admin.ModelAdmin):
    list_display = (
        "id", "surname", "name", "middle_name", "login_id",
        "group_display", "is_active",
    )
    list_filter = ("is_active", "group")
    search_fields = ("surname", "name", "middle_name", "login_id")
    autocomplete_fields = ("group",)
    readonly_fields = ("created_at", "updated_at")
    list_per_page = 50

    def group_display(self, obj):
        return obj.group.name if obj.group else "—"
    group_display.short_description = "Группа"


# =============================================================================
# Shift / OperatorScheduleDay
# =============================================================================

@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = (
        "code", "display_name", "start_time", "end_time",
        "crosses_midnight", "norm_full", "norm_lock_soft_cap",
        "is_night", "requires_special_pipeline", "is_active",
    )
    list_filter = ("is_active", "is_night", "crosses_midnight", "requires_special_pipeline")
    search_fields = ("code", "display_name")
    ordering = ("code",)


@admin.register(OperatorScheduleDay)
class OperatorScheduleDayAdmin(admin.ModelAdmin):
    list_display = ("operator", "day", "shift", "is_day_off", "source", "synced_at")
    list_filter = ("is_day_off", "source", "day")
    search_fields = (
        "operator__surname", "operator__name", "operator__login_id",
        "raw_value",
    )
    autocomplete_fields = ("operator", "shift")
    date_hierarchy = "day"
    list_per_page = 100


# =============================================================================
# Cycle
# =============================================================================

@admin.register(Cycle)
class CycleAdmin(admin.ModelAdmin):
    list_display = (
        "id", "year", "month", "start_date", "end_date",
        "status_colored", "opened_at", "closed_at",
    )
    list_filter = ("status", "year")
    search_fields = ("year", "month", "status")
    readonly_fields = ("opened_at", "closed_at", "closed_by", "archive_stats")
    ordering = ("-year", "-month")

    def status_colored(self, obj):
        colors = {
            "active": "green",
            "closing": "orange",
            "closed": "gray",
        }
        color = colors.get(obj.status, "black")
        return format_html(
            "<b style='color:{}'>{}</b>",
            color, obj.get_status_display(),
        )
    status_colored.short_description = "Статус"


# =============================================================================
# RequestTypeRule / SystemPolicy
# =============================================================================

class RequestSubtypeRuleInline(admin.TabularInline):
    model = RequestSubtypeRule
    extra = 0
    fields = (
        "code", "display_name", "full_day", "requires_hour_range",
        "sort_order", "is_active",
    )
    ordering = ("sort_order", "code")


@admin.register(RequestSubtypeRule)
class RequestSubtypeRuleAdmin(admin.ModelAdmin):
    list_display = (
        "type_rule", "code", "display_name",
        "full_day", "requires_hour_range", "sort_order", "is_active",
    )
    list_filter = ("type_rule", "is_active", "full_day", "requires_hour_range")
    search_fields = ("code", "display_name", "description")
    ordering = ("type_rule", "sort_order", "code")
    autocomplete_fields = ("type_rule",)


@admin.register(RequestTypeRule)
class RequestTypeRuleAdmin(admin.ModelAdmin):
    inlines = [RequestSubtypeRuleInline]
    list_display = (
        "category", "code", "display_name",
        "verification_strategy",
        "auto_approve_on_create", "exempts_from_daily_debt",
        "allows_past_date", "is_active",
    )
    list_filter = (
        "category", "is_active", "verification_strategy",
        "auto_approve_on_create", "exempts_from_daily_debt",
        "allows_past_date",
    )
    search_fields = ("code", "display_name", "description")
    ordering = ("category", "sort_order", "code")

    fieldsets = (
        (None, {
            "fields": ("category", "code", "display_name", "description", "is_active"),
        }),
        ("Обязательные поля", {
            "fields": (
                "requires_date_from", "requires_date_to",
                "requires_hour_range", "requires_duration",
                "requires_related_debts",
            ),
        }),
        ("Бизнес-логика", {
            "fields": (
                "creates_debt_if_unmet",
                "exempts_from_daily_debt",
                "auto_approve_on_create",
                "allows_past_date",
                "requires_supervisor_approval",
            ),
        }),
        ("Длительность", {
            "fields": ("min_duration", "max_duration"),
        }),
        ("Проверка", {
            "fields": ("verification_strategy", "sort_order"),
        }),
    )


@admin.register(SystemPolicy)
class SystemPolicyAdmin(admin.ModelAdmin):
    list_display = ("key", "value_preview", "valid_from", "valid_to", "updated_at")
    search_fields = ("key", "description")
    readonly_fields = ("created_at", "updated_at", "updated_by")

    def value_preview(self, obj):
        text = str(obj.value)
        return text[:80] + "..." if len(text) > 80 else text
    value_preview.short_description = "Значение"

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


# =============================================================================
# WorkLogDaily
# =============================================================================

@admin.register(WorkLogDaily)
class WorkLogDailyAdmin(admin.ModelAdmin):
    list_display = (
        "operator", "day", "shift_code_snapshot",
        "full_duration", "lock_duration",
        "is_special_aggregation", "loaded_at",
    )
    list_filter = ("day", "is_special_aggregation", "shift")
    search_fields = (
        "operator__surname", "operator__name", "operator__login_id",
    )
    autocomplete_fields = ("operator", "shift", "cycle")
    date_hierarchy = "day"
    readonly_fields = ("loaded_at", "created_at")
    list_per_page = 100


# =============================================================================
# WorkDebt / WorkDebtDetail
# =============================================================================

@admin.register(WorkDebt)
class WorkDebtAdmin(admin.ModelAdmin):
    list_display = (
        "operator", "cycle", "current_debt",
        "current_debt_days", "current_debt_hhmmss",
        "total_accumulated", "updated_at",
    )
    list_filter = ("cycle", "operator__group")
    search_fields = ("operator__surname", "operator__name", "operator__login_id")
    autocomplete_fields = ("operator", "cycle")
    readonly_fields = ("updated_at",)


@admin.register(WorkDebtDetail)
class WorkDebtDetailAdmin(admin.ModelAdmin):
    list_display = (
        "operator", "day", "source", "shift_code_snapshot",
        "violation_type", "debt_full", "debt_lock",
        "total_debt_display", "locked_for_compensation",
    )
    list_filter = (
        "day", "source", "violation_type",
        "locked_for_compensation", "shift",
    )
    search_fields = (
        "operator__surname", "operator__name", "operator__login_id",
    )
    autocomplete_fields = ("operator", "shift", "cycle")
    date_hierarchy = "day"
    readonly_fields = ("created_at", "updated_at")

    def total_debt_display(self, obj):
        return obj.total_debt
    total_debt_display.short_description = "Общий долг"


# =============================================================================
# Compensation
# =============================================================================

class CompensationDebtLinkInline(admin.TabularInline):
    model = CompensationDebtLink
    extra = 0
    autocomplete_fields = ("debt_detail",)
    readonly_fields = ("snapshot", "created_at", "updated_at")


class CompensationDayInline(admin.TabularInline):
    model = CompensationDay
    extra = 0
    readonly_fields = ("created_at", "updated_at")


@admin.register(Compensation)
class CompensationAdmin(admin.ModelAdmin):
    list_display = (
        "operator", "planned_date", "type_rule",
        "source", "status_colored", "requested_duration",
        "verified_duration", "deducted", "verified_at",
    )
    list_filter = (
        "status", "source", "type_rule", "deducted",
        "planned_date",
    )
    search_fields = (
        "operator__surname", "operator__name", "operator__login_id",
        "comment",
    )
    autocomplete_fields = ("operator", "type_rule", "cycle", "fixed_by", "verified_by")
    date_hierarchy = "planned_date"
    readonly_fields = (
        "created_at", "updated_at", "verified_at",
        "auto_check_result", "auto_check_at", "debts_snapshot",
    )
    inlines = [CompensationDayInline, CompensationDebtLinkInline]

    def status_colored(self, obj):
        colors = {
            "pending": "orange",
            "approved": "green",
            "partial": "blue",
            "declined": "red",
        }
        color = colors.get(obj.status, "black")
        return format_html(
            "<b style='color:{}'>{}</b>",
            color, obj.get_status_display(),
        )
    status_colored.short_description = "Статус"


# =============================================================================
# Transfer
# =============================================================================

@admin.register(Transfer)
class TransferAdmin(admin.ModelAdmin):
    list_display = (
        "operator", "type_rule", "status_colored",
        "date_from", "date_to", "requested_duration",
        "verified_duration", "verified_at",
    )
    list_filter = ("status", "type_rule", "date_from", "date_to")
    search_fields = (
        "operator__surname", "operator__name", "operator__login_id",
        "comment",
    )
    autocomplete_fields = ("operator", "type_rule", "cycle", "fixed_by", "verified_by")
    date_hierarchy = "date_from"
    readonly_fields = ("created_at", "updated_at", "verified_at")

    def status_colored(self, obj):
        colors = {
            "pending": "orange",
            "in_progress": "blue",
            "approved": "green",
            "partial": "purple",
            "completed": "gray",
            "declined": "red",
        }
        color = colors.get(obj.status, "black")
        return format_html(
            "<b style='color:{}'>{}</b>",
            color, obj.get_status_display(),
        )
    status_colored.short_description = "Статус"


# =============================================================================
# ManualAdjustment / AutomationOverride / Note
# =============================================================================

@admin.register(ManualAdjustment)
class ManualAdjustmentAdmin(admin.ModelAdmin):
    list_display = (
        "id", "target_type", "target_id", "field_name",
        "operator", "reason_code", "adjusted_by", "adjusted_at",
        "approved_by",
    )
    list_filter = ("target_type", "reason_code", "adjusted_at")
    search_fields = (
        "field_name", "reason_text",
        "adjusted_by__username", "operator__login_id",
    )
    readonly_fields = (
        "adjusted_at", "approved_at",
        "old_value", "new_value",
    )
    autocomplete_fields = ("operator", "adjusted_by", "approved_by")
    date_hierarchy = "adjusted_at"


@admin.register(AutomationOverride)
class AutomationOverrideAdmin(admin.ModelAdmin):
    list_display = (
        "operator", "override_type", "valid_from", "valid_to",
        "is_active", "created_by",
    )
    list_filter = ("override_type", "is_active", "valid_from")
    search_fields = ("operator__surname", "operator__login_id", "reason")
    autocomplete_fields = ("operator", "created_by", "deactivated_by")
    readonly_fields = ("created_at", "deactivated_at")


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    list_display = (
        "id", "target_type", "target_id", "visibility",
        "created_by", "created_at",
    )
    list_filter = ("target_type", "visibility", "created_at")
    search_fields = ("text", "created_by__username")
    readonly_fields = ("created_at", "updated_at", "created_by")


# =============================================================================
# EventLog
# =============================================================================

@admin.register(EventLog)
class EventLogAdmin(admin.ModelAdmin):
    list_display = (
        "timestamp", "level", "event_type", "operator",
        "target_type", "target_id", "message_short",
    )
    list_filter = ("level", "event_type", "target_type")
    search_fields = ("message", "operator__login_id")
    readonly_fields = (
        "event_type", "level", "operator", "cycle",
        "target_type", "target_id", "message", "payload",
        "timestamp", "triggered_by",
    )
    date_hierarchy = "timestamp"
    list_per_page = 100

    def message_short(self, obj):
        return obj.message[:80] + "..." if obj.message and len(obj.message) > 80 else obj.message
    message_short.short_description = "Сообщение"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False



@admin.register(RequestActionLog)
class RequestActionLogAdmin(admin.ModelAdmin):
    """Аудит-лог действий над заявками (read-only; создаётся в WFM, push в Django)."""
    list_display = (
        "created_at", "user_name", "method", "entity_type",
        "entity_external_id", "status_code", "success",
    )
    list_filter = ("method", "entity_type", "success")
    search_fields = ("user_name", "user_id", "endpoint", "entity_external_id", "wfm_id")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = (
        "summary_display",
        "wfm_id", "user_id", "user_name", "method", "entity_type",
        "entity_external_id", "entity_wfm_id", "endpoint", "payload", "status_code",
        "success", "created_at",
    )
    fields = (
        "summary_display",
        "wfm_id", "user_id", "user_name", "method", "entity_type",
        "entity_external_id", "entity_wfm_id", "endpoint", "status_code", "success",
        "created_at", "payload",
    )

    @admin.display(description="Данный")
    def summary_display(self, obj):
        from .services.action_log_format import format_action_log
        return format_html(
            "<pre style='white-space:pre-wrap;margin:0;font-family:inherit;"
            "font-size:13px;line-height:1.6'>{}</pre>",
            format_action_log(obj),
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
