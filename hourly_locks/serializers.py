"""
DRF-сериализаторы для всех моделей hourly_locks.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

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
    RequestTypeRule,
    Shift,
    SystemPolicy,
    Transfer,
    User,
    WorkDebt,
    WorkDebtDetail,
    WorkLogDaily,
)
from .utils import format_duration


# =============================================================================
# Group / Operator / User
# =============================================================================

class GroupShortSerializer(serializers.ModelSerializer):
    supervisor_name = serializers.SerializerMethodField()

    class Meta:
        model = Group
        fields = ["id", "name", "supervisor", "supervisor_name", "is_active"]

    def get_supervisor_name(self, obj):
        if not obj.supervisor:
            return None
        sup = obj.supervisor
        op = getattr(sup, "operator", None)
        if op:
            return f"{op.surname} {op.name}"
        return sup.username


class GroupSerializer(GroupShortSerializer):
    operators_count = serializers.SerializerMethodField()
    operators = serializers.SerializerMethodField()

    class Meta(GroupShortSerializer.Meta):
        fields = GroupShortSerializer.Meta.fields + [
            "operators_count", "operators", "created_at",
        ]

    def get_operators_count(self, obj):
        return obj.operators.filter(is_active=True).count()

    def get_operators(self, obj):
        return [
            {
                "id": op.id,
                "login_id": op.login_id,
                "name": op.full_name,
            }
            for op in obj.operators.filter(is_active=True)[:50]
        ]


class OperatorMiniSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = Operator
        fields = ["id", "login_id", "surname", "name", "middle_name", "full_name"]


class OperatorLightSerializer(serializers.ModelSerializer):
    group_name = serializers.CharField(source="group.name", read_only=True, default=None)
    role = serializers.CharField(read_only=True)

    class Meta:
        model = Operator
        fields = [
            "id", "login_id", "surname", "name", "middle_name",
            "group_name", "role", "is_active",
        ]


class OperatorSerializer(serializers.ModelSerializer):
    group = GroupShortSerializer(read_only=True)
    role = serializers.CharField(read_only=True)
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = Operator
        fields = "__all__"


class UserShortSerializer(serializers.ModelSerializer):
    operator = OperatorMiniSerializer(read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "role", "operator"]


class UserMeSerializer(serializers.ModelSerializer):
    operator = OperatorSerializer(read_only=True)
    supervised_groups = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "username", "first_name", "last_name", "email",
            "role", "operator", "supervised_groups",
        ]

    def get_supervised_groups(self, obj):
        return GroupShortSerializer(
            obj.supervised_groups.all(), many=True,
        ).data


# =============================================================================
# Shift / OperatorScheduleDay
# =============================================================================

class ShiftSerializer(serializers.ModelSerializer):
    norm_full_str = serializers.SerializerMethodField()
    norm_lock_soft_cap_str = serializers.SerializerMethodField()

    class Meta:
        model = Shift
        fields = "__all__"

    def get_norm_full_str(self, obj):
        return format_duration(obj.norm_full)

    def get_norm_lock_soft_cap_str(self, obj):
        return format_duration(obj.norm_lock_soft_cap)


class OperatorScheduleDaySerializer(serializers.ModelSerializer):
    operator = OperatorLightSerializer(read_only=True)
    operator_id = serializers.PrimaryKeyRelatedField(
        queryset=Operator.objects.all(), source="operator", write_only=True,
    )
    shift = ShiftSerializer(read_only=True)
    shift_id = serializers.PrimaryKeyRelatedField(
        queryset=Shift.objects.all(), source="shift",
        write_only=True, allow_null=True, required=False,
    )

    class Meta:
        model = OperatorScheduleDay
        fields = "__all__"


# =============================================================================
# Cycle
# =============================================================================

class CycleSerializer(serializers.ModelSerializer):
    closed_by_username = serializers.CharField(
        source="closed_by.username", read_only=True, default=None,
    )

    class Meta:
        model = Cycle
        fields = "__all__"
        read_only_fields = [
            "opened_at", "closed_at", "closed_by",
            "archive_stats", "status",
        ]


# =============================================================================
# RequestTypeRule / SystemPolicy
# =============================================================================

class RequestTypeRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = RequestTypeRule
        fields = "__all__"


class SystemPolicySerializer(serializers.ModelSerializer):
    updated_by_username = serializers.CharField(
        source="updated_by.username", read_only=True, default=None,
    )

    class Meta:
        model = SystemPolicy
        fields = "__all__"
        read_only_fields = ["updated_by", "created_at", "updated_at"]


# =============================================================================
# WorkLogDaily
# =============================================================================

class WorkLogDailySerializer(serializers.ModelSerializer):
    operator = OperatorLightSerializer(read_only=True)
    shift_code = serializers.CharField(source="shift_code_snapshot", read_only=True)
    full_duration_str = serializers.SerializerMethodField()
    lock_duration_str = serializers.SerializerMethodField()
    net_duration = serializers.DurationField(read_only=True)

    class Meta:
        model = WorkLogDaily
        fields = "__all__"

    def get_full_duration_str(self, obj):
        return format_duration(obj.full_duration)

    def get_lock_duration_str(self, obj):
        return format_duration(obj.lock_duration)


# =============================================================================
# WorkDebt / WorkDebtDetail
# =============================================================================

class WorkDebtSerializer(serializers.ModelSerializer):
    operator = OperatorLightSerializer(read_only=True)
    cycle = CycleSerializer(read_only=True)
    current_debt_str = serializers.SerializerMethodField()
    total_accumulated_str = serializers.SerializerMethodField()
    current_debt_days = serializers.IntegerField(read_only=True)
    current_debt_hhmmss = serializers.CharField(read_only=True)

    class Meta:
        model = WorkDebt
        fields = "__all__"

    def get_current_debt_str(self, obj):
        return format_duration(obj.current_debt)

    def get_total_accumulated_str(self, obj):
        return format_duration(obj.total_accumulated)


class WorkDebtListLightSerializer(serializers.ModelSerializer):
    """Облегчённый сериализатор для списка долгов."""
    operator = OperatorLightSerializer(read_only=True)
    current_debt_days = serializers.IntegerField(read_only=True)
    current_debt_hhmmss = serializers.CharField(read_only=True)

    class Meta:
        model = WorkDebt
        fields = [
            "id", "operator", "current_debt",
            "current_debt_days", "current_debt_hhmmss",
        ]


class WorkDebtDetailShortSerializer(serializers.ModelSerializer):
    operator = OperatorLightSerializer(read_only=True)
    total_debt = serializers.DurationField(read_only=True)
    total_debt_str = serializers.SerializerMethodField()

    class Meta:
        model = WorkDebtDetail
        fields = [
            "id", "operator", "day", "source",
            "shift_code_snapshot", "violation_type",
            "norm_full", "fact_full", "debt_full",
            "norm_lock", "fact_lock", "debt_lock",
            "total_debt", "total_debt_str",
            "make_up_wh", "locked_for_compensation",
            "note", "created_at",
        ]

    def get_total_debt_str(self, obj):
        return format_duration(obj.total_debt)


class WorkDebtDetailSerializer(serializers.ModelSerializer):
    operator = OperatorLightSerializer(read_only=True)
    cycle = CycleSerializer(read_only=True)
    total_debt = serializers.DurationField(read_only=True)

    class Meta:
        model = WorkDebtDetail
        fields = "__all__"


# =============================================================================
# CompensationDebtLink
# =============================================================================

class CompensationDebtLinkSerializer(serializers.ModelSerializer):
    debt_detail = WorkDebtDetailShortSerializer(read_only=True)

    class Meta:
        model = CompensationDebtLink
        fields = "__all__"


# =============================================================================
# Compensation
# =============================================================================

class CompensationSerializer(serializers.ModelSerializer):
    """Для list/create — облегчённый."""
    operator = OperatorLightSerializer(read_only=True)
    operator_id = serializers.PrimaryKeyRelatedField(
        queryset=Operator.objects.all(),
        source="operator", write_only=True,
    )
    type_rule = RequestTypeRuleSerializer(read_only=True)
    type_rule_id = serializers.PrimaryKeyRelatedField(
        queryset=RequestTypeRule.objects.filter(category="compensation"),
        source="type_rule", write_only=True,
    )
    cycle = CycleSerializer(read_only=True)
    fixed_by_name = serializers.SerializerMethodField()

    related_debts_input = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True, required=False,
        help_text="ID записей WorkDebtDetail, покрываемых этой заявкой",
    )
    auto_sum_from_debts = serializers.BooleanField(
        write_only=True, required=False, default=True,
        help_text=(
            "Если True (по умолчанию) и related_debts_input не пуст — "
            "requested_duration рассчитывается как сумма debt_full+debt_lock "
            "выбранных записей долга. Если False — берётся переданное значение."
        ),
    )

    class Meta:
        model = Compensation
        fields = "__all__"
        read_only_fields = [
            "verified_at", "verified_by", "fixed_by",
            "deducted", "verified_duration", "remaining_debt",
            "auto_check_result", "auto_check_at",
            # debts_snapshot avtomatik to'ldiriladi (related_debts_input dan)
            "debts_snapshot",
        ]
        extra_kwargs = {
            # claim_metadata faqat retroactive uchun. Bo'sh bo'lsa default={}
            "claim_metadata": {"required": False, "default": dict, "allow_null": False},
            # status front'dan o'tkazilmasligi kerak (default pending)
            "status": {"required": False},
        }

    def get_fixed_by_name(self, obj):
        if not obj.fixed_by:
            return None
        op = getattr(obj.fixed_by, "operator", None)
        return f"{op.surname} {op.name}" if op else obj.fixed_by.username

    def validate(self, attrs):
        # Прогон через model.clean()
        # Faqat model maydonlarini uzatamiz (write_only yordamchi field'larni o'tkazib yuboramiz)
        NON_MODEL_FIELDS = {"related_debts_input", "auto_sum_from_debts"}
        model_attrs = {k: v for k, v in attrs.items() if k not in NON_MODEL_FIELDS}
        instance = Compensation(**model_attrs)
        try:
            instance.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)
        return attrs

    def create(self, validated_data):
        from .services.cycle import get_or_create_active_cycle

        validated_data.pop("related_debts_input", None)
        auto_sum = validated_data.pop("auto_sum_from_debts", True)
        request = self.context.get("request")

        if request and hasattr(request, "user"):
            validated_data["fixed_by"] = request.user

        if "cycle" not in validated_data:
            validated_data["cycle"] = get_or_create_active_cycle()

        # NULL bo'lmasin — JSON maydonlarga defaultni majburlash
        if validated_data.get("claim_metadata") is None:
            validated_data["claim_metadata"] = {}
        if validated_data.get("debts_snapshot") is None:
            validated_data["debts_snapshot"] = []
        if validated_data.get("auto_check_result") is None:
            validated_data["auto_check_result"] = {}

        # Парсим related_debts_input
        related_debts_input = self.initial_data.get("related_debts_input") or []
        if isinstance(related_debts_input, str):
            import json
            try:
                related_debts_input = json.loads(related_debts_input)
            except Exception:
                related_debts_input = []

        # Авто-расчёт requested_duration если есть связанные долги
        if auto_sum and related_debts_input:
            total = self._calc_total_debt_duration(related_debts_input)
            if total > timedelta(0):
                validated_data["requested_duration"] = total

        compensation = Compensation.objects.create(**validated_data)

        if related_debts_input:
            self._create_debt_links(compensation, related_debts_input)

        return compensation

    def _calc_total_debt_duration(self, debt_ids: list) -> timedelta:
        """Сумма debt_full+debt_lock по выбранным WorkDebtDetail."""
        from datetime import timedelta as td
        debts = WorkDebtDetail.objects.filter(id__in=debt_ids)
        total = td(0)
        for d in debts:
            total += (d.debt_full or td(0)) + (d.debt_lock or td(0))
        return total

    def _create_debt_links(self, compensation: Compensation, debt_ids: list):
        from datetime import timedelta as td

        debts = WorkDebtDetail.objects.filter(id__in=debt_ids)
        snapshot = []
        for d in debts:
            snap = {
                "id": d.id,
                "day": str(d.day),
                "source": d.source,
                "shift_code": d.shift_code_snapshot,
                "norm_full": str(d.norm_full),
                "fact_full": str(d.fact_full),
                "debt_full": str(d.debt_full),
                "norm_lock": str(d.norm_lock),
                "fact_lock": str(d.fact_lock),
                "debt_lock": str(d.debt_lock),
                "note": d.note or "",
            }
            CompensationDebtLink.objects.create(
                compensation=compensation,
                debt_detail=d,
                snapshot=snap,
                applied=False,
            )
            snapshot.append(snap)

        compensation.debts_snapshot = snapshot
        compensation.save(update_fields=["debts_snapshot", "updated_at"])


class CompensationDaySerializer(serializers.ModelSerializer):
    """День отработки в составе заявки."""

    class Meta:
        model = CompensationDay
        fields = [
            "id", "day", "allocated_duration", "status",
            "credited_duration", "fact_full", "fact_lock",
            "verified_at", "note",
        ]


class CompensationDetailSerializer(CompensationSerializer):
    """Для retrieve — с CompensationDebtLink и днями отработки."""
    debt_links = CompensationDebtLinkSerializer(many=True, read_only=True)
    days = CompensationDaySerializer(many=True, read_only=True)


# =============================================================================
# Отработка (otrabotka) — приём заявки
# =============================================================================

class OtrabotkaAcceptSerializer(serializers.Serializer):
    """
    Вход для preview/accept заявки отработки.

    Сумма выбранных долгов считается автоматически. durations — необязательный
    список длительностей ПО ДНЯМ (выровнен с days, каждый день редактируется на
    фронте): каждый CompensationDay получает свою длительность, requested_duration
    = их сумма. Сумма может быть МЕНЬШЕ суммы долгов (частичная отработка) —
    непокрытый остаток при верификации возвращается в открытый долг; оригинальная
    сумма долгов доступна в claim_metadata.debt_total. Если durations не задан —
    сумма долгов делится поровну. Дни — в пределах активного цикла.
    """
    operator_id = serializers.IntegerField()
    debt_detail_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False,
    )
    days = serializers.ListField(
        child=serializers.DateField(), allow_empty=False,
    )
    durations = serializers.ListField(
        child=serializers.DurationField(), required=False, allow_null=True,
        allow_empty=True,
    )
    comment = serializers.CharField(
        required=False, allow_blank=True, default="",
    )
    pdf_file = serializers.FileField(required=False, allow_null=True)
    screens = serializers.ImageField(required=False, allow_null=True)

    def validate_operator_id(self, value):
        if not Operator.objects.filter(id=value).exists():
            raise serializers.ValidationError("Оператор не найден.")
        return value


class DebtCompensationAcceptSerializer(serializers.Serializer):
    """
    Вход для preview/accept прямого списания долга компенсацией
    (любой тип compensation, КРОМЕ otrabotka).

    applied_duration — необязательно: списываемая сумма. Если не задана —
    списывается вся сумма выбранных долгов; если меньше — на остаток
    создаётся одна запись долга.
    """
    operator_id = serializers.IntegerField()
    code = serializers.CharField()
    debt_detail_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False,
    )
    applied_duration = serializers.DurationField(required=False, allow_null=True)
    comment = serializers.CharField(
        required=False, allow_blank=True, default="",
    )
    pdf_file = serializers.FileField(required=False, allow_null=True)
    screens = serializers.ImageField(required=False, allow_null=True)

    def validate_operator_id(self, value):
        if not Operator.objects.filter(id=value).exists():
            raise serializers.ValidationError("Оператор не найден.")
        return value

    def validate_code(self, value):
        if value == "otrabotka":
            raise serializers.ValidationError(
                "Для отработки используйте endpoint otrabotka."
            )
        return value


class OtprashivanieAcceptSerializer(serializers.Serializer):
    """
    Вход для preview/accept заявки отпрашивания (Transfer, тип otprashivanie).

    Один/несколько операторов, один/несколько РАБОЧИХ дней отгула в окне
    hour_from–hour_to, и дни отработки (отпрошенные часы делятся по ним).
    """
    operator_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False,
    )
    days = serializers.ListField(
        child=serializers.DateField(), allow_empty=False,
    )
    hour_from = serializers.TimeField()
    hour_to = serializers.TimeField()
    workoff_days = serializers.ListField(
        child=serializers.DateField(), allow_empty=False,
    )
    comment = serializers.CharField(required=False, allow_blank=True, default="")
    pdf_file = serializers.FileField(required=False, allow_null=True)
    screens = serializers.ImageField(required=False, allow_null=True)


class UchebaAcceptSerializer(serializers.Serializer):
    """
    Вход для preview/accept заявки «Учёба в рабочее время» (Transfer).
    Один/несколько операторов, рабочие дни, окно учёбы (hour_from–hour_to).
    """
    operator_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False,
    )
    days = serializers.ListField(
        child=serializers.DateField(), allow_empty=False,
    )
    hour_from = serializers.TimeField()
    hour_to = serializers.TimeField()
    comment = serializers.CharField(required=False, allow_blank=True, default="")
    pdf_file = serializers.FileField(required=False, allow_null=True)
    screens = serializers.ImageField(required=False, allow_null=True)


class BenefitAcceptSerializer(serializers.Serializer):
    """
    Вход для preview/accept заявки-льготы (Исключение/Обучение/Льготы/Хоз.работы).

    Операторы: явный список operator_ids ИЛИ select_all_shift ("9h"/"12h").
    Дни — список (только рабочие). Часы (hour_from/hour_to) — необязательно:
    без часов = весь день; с часами = частичное освобождение.
    """
    operator_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True, default=list,
    )
    select_all_shift = serializers.ChoiceField(
        choices=["9h"], required=False, allow_null=True,
    )
    code = serializers.CharField()
    subtype = serializers.ChoiceField(
        choices=["otgul", "korotkiy_den"], required=False, allow_null=True,
    )
    days = serializers.ListField(
        child=serializers.DateField(), allow_empty=False,
    )
    hour_from = serializers.TimeField(required=False, allow_null=True)
    hour_to = serializers.TimeField(required=False, allow_null=True)
    comment = serializers.CharField(required=False, allow_blank=True, default="")
    pdf_file = serializers.FileField(required=False, allow_null=True)
    screens = serializers.ImageField(required=False, allow_null=True)

    def validate(self, attrs):
        if not attrs.get("operator_ids") and not attrs.get("select_all_shift"):
            raise serializers.ValidationError(
                "Укажите operator_ids или select_all_shift.")
        return attrs


# =============================================================================
# Transfer
# =============================================================================

class TransferSerializer(serializers.ModelSerializer):
    operator = OperatorLightSerializer(read_only=True)
    operator_id = serializers.PrimaryKeyRelatedField(
        queryset=Operator.objects.all(),
        source="operator", write_only=True,
    )
    type_rule = RequestTypeRuleSerializer(read_only=True)
    type_rule_id = serializers.PrimaryKeyRelatedField(
        queryset=RequestTypeRule.objects.filter(category="transfer"),
        source="type_rule", write_only=True,
    )
    cycle = CycleSerializer(read_only=True)
    fixed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Transfer
        fields = "__all__"
        read_only_fields = [
            "verified_at", "verified_by", "fixed_by",
            "verified_duration", "remaining_debt",
        ]

    def get_fixed_by_name(self, obj):
        if not obj.fixed_by:
            return None
        op = getattr(obj.fixed_by, "operator", None)
        return f"{op.surname} {op.name}" if op else obj.fixed_by.username

    def validate(self, attrs):
        instance = Transfer(**attrs)
        try:
            instance.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)
        return attrs

    def create(self, validated_data):
        from .services.cycle import get_or_create_active_cycle

        request = self.context.get("request")
        if request and hasattr(request, "user"):
            validated_data["fixed_by"] = request.user

        if "cycle" not in validated_data:
            validated_data["cycle"] = get_or_create_active_cycle()

        return Transfer.objects.create(**validated_data)


# =============================================================================
# ManualAdjustment / AutomationOverride / Note
# =============================================================================

class ManualAdjustmentSerializer(serializers.ModelSerializer):
    adjusted_by_username = serializers.CharField(
        source="adjusted_by.username", read_only=True,
    )
    approved_by_username = serializers.CharField(
        source="approved_by.username", read_only=True, default=None,
    )
    operator = OperatorLightSerializer(read_only=True)

    class Meta:
        model = ManualAdjustment
        fields = "__all__"
        read_only_fields = [
            "adjusted_by", "adjusted_at",
            "approved_by", "approved_at",
        ]


class AutomationOverrideSerializer(serializers.ModelSerializer):
    operator = OperatorLightSerializer(read_only=True)
    operator_id = serializers.PrimaryKeyRelatedField(
        queryset=Operator.objects.all(),
        source="operator", write_only=True,
    )
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True,
    )

    class Meta:
        model = AutomationOverride
        fields = "__all__"
        read_only_fields = [
            "created_by", "created_at",
            "deactivated_by", "deactivated_at",
        ]


class NoteSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True,
    )

    class Meta:
        model = Note
        fields = "__all__"
        read_only_fields = ["created_by", "created_at", "updated_at"]


# =============================================================================
# EventLog (read-only)
# =============================================================================

class EventLogSerializer(serializers.ModelSerializer):
    operator = OperatorLightSerializer(read_only=True)
    triggered_by_username = serializers.CharField(
        source="triggered_by.username", read_only=True, default=None,
    )

    class Meta:
        model = EventLog
        fields = "__all__"


class MyTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["username"] = user.username
        token["role"] = user.role
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserMeSerializer(self.user).data
        return data


# =============================================================================
# Ретроактивная проверка (упрощённый сериализатор)
# =============================================================================

class RetroactiveCheckRequestSerializer(serializers.Serializer):
    """Входной сериализатор для ретроактивной проверки."""
    operator_id = serializers.IntegerField()
    planned_date = serializers.DateField()
    requested_duration = serializers.DurationField()
    type_rule_code = serializers.CharField(default="retroactive_compensation")
    reason_text = serializers.CharField(required=False, allow_blank=True)
    witnesses = serializers.CharField(required=False, allow_blank=True)