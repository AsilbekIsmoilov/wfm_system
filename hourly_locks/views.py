"""
DRF-ViewSet'ы и APIView для hourly_locks.
"""

import logging
from datetime import date, datetime, timedelta

from django.conf import settings
from django.db.models import Q
from django.utils.timezone import now
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from .filters import (
    CompensationFilter,
    EventLogFilter,
    ManualAdjustmentFilter,
    OperatorScheduleDayFilter,
    TransferFilter,
    WorkDebtDetailFilter,
    WorkLogDailyFilter,
)
from .models import (
    AutomationOverride,
    Compensation,
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
from .pagination import LargePagination, StandardPagination, TransferPagination
from .permissions import (
    CanCloseCycle,
    CanManualAdjust,
    IsManagerOrAdmin,
    IsSupervisorOrHigher,
    ReadOnlyOrManager,
)
from .serializers import (
    AutomationOverrideSerializer,
    CompensationDetailSerializer,
    CompensationSerializer,
    CycleSerializer,
    BenefitAcceptSerializer,
    DebtCompensationAcceptSerializer,
    OtprashivanieAcceptSerializer,
    OtrabotkaAcceptSerializer,
    UchebaAcceptSerializer,
    EventLogSerializer,
    GroupSerializer,
    ManualAdjustmentSerializer,
    MyTokenObtainPairSerializer,
    NoteSerializer,
    OperatorScheduleDaySerializer,
    OperatorSerializer,
    RequestTypeRuleSerializer,
    RetroactiveCheckRequestSerializer,
    ShiftSerializer,
    SystemPolicySerializer,
    TransferSerializer,
    UserMeSerializer,
    WorkDebtDetailSerializer,
    WorkDebtDetailShortSerializer,
    WorkDebtListLightSerializer,
    WorkDebtSerializer,
    WorkLogDailySerializer,
)

logger = logging.getLogger(__name__)


# =============================================================================
# JWT и Me
# =============================================================================

class MyTokenObtainPairView(TokenObtainPairView):
    serializer_class = MyTokenObtainPairSerializer


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = (
            User.objects
            .select_related("operator", "operator__group")
            .prefetch_related("supervised_groups")
            .get(pk=request.user.id)
        )
        return Response(UserMeSerializer(user).data)


# =============================================================================
# Operator / Group
# =============================================================================

class OperatorViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = OperatorSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["surname", "name", "middle_name", "login_id"]
    pagination_class = StandardPagination

    def get_queryset(self):
        user = self.request.user
        qs = Operator.objects.filter(is_active=True).select_related("group")

        if user.role == "operator":
            op = getattr(user, "operator", None)
            qs = qs.filter(id=op.id) if op else qs.none()
        elif user.role == "supervisor":
            op = getattr(user, "operator", None)
            if op:
                group_ids = user.supervised_groups.values_list("id", flat=True)
                qs = qs.filter(group_id__in=group_ids)
            else:
                qs = qs.none()

        return qs.order_by("surname", "name")


class GroupViewSet(viewsets.ModelViewSet):
    queryset = Group.objects.filter(is_active=True).select_related("supervisor")
    serializer_class = GroupSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardPagination


# =============================================================================
# Shift
# =============================================================================

class ShiftViewSet(viewsets.ModelViewSet):
    queryset = Shift.objects.all().order_by("code")
    serializer_class = ShiftSerializer
    permission_classes = [ReadOnlyOrManager]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["code", "display_name"]
    filterset_fields = ["is_active", "is_night", "requires_special_pipeline"]


# =============================================================================
# OperatorScheduleDay
# =============================================================================

class OperatorScheduleDayViewSet(viewsets.ModelViewSet):
    serializer_class = OperatorScheduleDaySerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = OperatorScheduleDayFilter
    pagination_class = StandardPagination

    def get_queryset(self):
        user = self.request.user
        qs = OperatorScheduleDay.objects.select_related(
            "operator", "operator__group", "shift",
        ).order_by("-day")

        if user.role == "operator":
            op = getattr(user, "operator", None)
            qs = qs.filter(operator=op) if op else qs.none()
        elif user.role == "supervisor":
            group_ids = user.supervised_groups.values_list("id", flat=True)
            qs = qs.filter(operator__group_id__in=group_ids)

        return qs


# =============================================================================
# Cycle
# =============================================================================

class CycleViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Cycle.objects.all().order_by("-year", "-month")
    serializer_class = CycleSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardPagination

    @action(detail=False, methods=["get"], url_path="active")
    def active(self, request):
        """Возвращает текущий активный цикл."""
        active_cycle = Cycle.get_active()
        if not active_cycle:
            return Response({"detail": "Активный цикл не найден"}, status=404)
        return Response(self.get_serializer(active_cycle).data)

    @action(
        detail=True, methods=["post"],
        url_path="close", permission_classes=[CanCloseCycle],
    )
    def close(self, request, pk=None):
        """Ручное закрытие цикла (только manager/admin)."""
        from .services.cycle import close_cycle

        cycle = self.get_object()
        if cycle.status == "closed":
            return Response(
                {"detail": "Цикл уже закрыт"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = close_cycle(cycle, user=request.user)
        return Response(result)


# =============================================================================
# RequestTypeRule / SystemPolicy
# =============================================================================

class RequestTypeRuleViewSet(viewsets.ModelViewSet):
    queryset = RequestTypeRule.objects.all().order_by("category", "sort_order")
    serializer_class = RequestTypeRuleSerializer
    permission_classes = [ReadOnlyOrManager]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["category", "is_active", "verification_strategy"]


class SystemPolicyViewSet(viewsets.ModelViewSet):
    queryset = SystemPolicy.objects.all().order_by("key")
    serializer_class = SystemPolicySerializer
    permission_classes = [IsManagerOrAdmin]

    def perform_create(self, serializer):
        serializer.save(updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)


# =============================================================================
# WorkLogDaily
# =============================================================================

class WorkLogDailyViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = WorkLogDailySerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = WorkLogDailyFilter
    pagination_class = StandardPagination

    def get_queryset(self):
        user = self.request.user
        qs = WorkLogDaily.objects.select_related(
            "operator", "operator__group", "shift", "cycle",
        ).order_by("-day")

        if user.role == "operator":
            op = getattr(user, "operator", None)
            qs = qs.filter(operator=op) if op else qs.none()
        elif user.role == "supervisor":
            group_ids = user.supervised_groups.values_list("id", flat=True)
            qs = qs.filter(operator__group_id__in=group_ids)

        return qs


# =============================================================================
# WorkDebt / WorkDebtDetail
# =============================================================================

class WorkDebtViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = WorkDebtSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["operator", "cycle"]
    pagination_class = StandardPagination

    def get_queryset(self):
        user = self.request.user
        qs = WorkDebt.objects.select_related(
            "operator", "operator__group", "cycle",
        )

        if user.role == "operator":
            op = getattr(user, "operator", None)
            qs = qs.filter(operator=op) if op else qs.none()
        elif user.role == "supervisor":
            group_ids = user.supervised_groups.values_list("id", flat=True)
            qs = qs.filter(operator__group_id__in=group_ids)

        return qs.order_by("operator__surname")

    @action(detail=False, methods=["get"], url_path="light")
    def light(self, request):
        """Облегчённый список (для главного экрана)."""
        qs = self.get_queryset()
        serializer = WorkDebtListLightSerializer(
            qs, many=True, context={"request": request},
        )
        return Response(serializer.data)


class WorkDebtDetailViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = WorkDebtDetailSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = WorkDebtDetailFilter
    pagination_class = StandardPagination

    def get_queryset(self):
        user = self.request.user
        qs = WorkDebtDetail.objects.select_related(
            "operator", "operator__group", "shift", "cycle",
        ).order_by("-day")

        if user.role == "operator":
            op = getattr(user, "operator", None)
            qs = qs.filter(operator=op) if op else qs.none()
        elif user.role == "supervisor":
            group_ids = user.supervised_groups.values_list("id", flat=True)
            qs = qs.filter(operator__group_id__in=group_ids)

        return qs

    def get_serializer_class(self):
        if self.action == "list":
            return WorkDebtDetailShortSerializer
        return WorkDebtDetailSerializer


# =============================================================================
# Compensation
# =============================================================================

class CompensationViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    parser_classes = (JSONParser, MultiPartParser, FormParser)
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = CompensationFilter
    search_fields = [
        "operator__surname", "operator__name", "operator__login_id",
        "comment",
    ]
    ordering_fields = ["planned_date", "created_at", "status"]
    pagination_class = TransferPagination

    def get_queryset(self):
        user = self.request.user
        qs = Compensation.objects.select_related(
            "operator", "operator__group", "type_rule", "cycle",
            "fixed_by", "verified_by",
        ).prefetch_related("debt_links__debt_detail")

        if user.role == "operator":
            op = getattr(user, "operator", None)
            qs = qs.filter(operator=op) if op else qs.none()
        elif user.role == "supervisor":
            group_ids = user.supervised_groups.values_list("id", flat=True)
            qs = qs.filter(operator__group_id__in=group_ids)

        return qs.order_by("-planned_date", "-created_at")

    def get_serializer_class(self):
        if self.action == "retrieve":
            return CompensationDetailSerializer
        return CompensationSerializer

    def perform_create(self, serializer):
        from .services.compensation_verifier import verify_single_compensation
        from .services.cycle import get_or_create_active_cycle

        comp = serializer.save()

        # Если тип имеет auto_approve_on_create — обрабатываем
        rule = comp.type_rule
        if rule.auto_approve_on_create:
            cycle = get_or_create_active_cycle()
            verify_single_compensation(comp, comp.planned_date, cycle)

    # ------------------------------------------------------------------ #
    # Отработка (otrabotka) — приём заявки
    # ------------------------------------------------------------------ #

    def _resolve_otrabotka_operator(self, request, operator_id):
        """Найти оператора с проверкой прав. Возвращает (operator, error_response)."""
        try:
            operator = Operator.objects.get(id=operator_id)
        except Operator.DoesNotExist:
            return None, Response(
                {"detail": "Оператор не найден"},
                status=status.HTTP_404_NOT_FOUND,
            )

        user = request.user
        if user.role == "operator":
            op = getattr(user, "operator", None)
            if not op or op.id != operator.id:
                return None, Response(
                    {"detail": "Нельзя подавать заявку за другого оператора"},
                    status=status.HTTP_403_FORBIDDEN,
                )
        elif user.role == "supervisor":
            group_ids = set(user.supervised_groups.values_list("id", flat=True))
            if operator.group_id not in group_ids:
                return None, Response(
                    {"detail": "Оператор вне ваших групп"},
                    status=status.HTTP_403_FORBIDDEN,
                )
        return operator, None

    @action(detail=False, methods=["get"], url_path="otrabotka/disabled-days")
    def otrabotka_disabled_days(self, request):
        """Дни, недоступные для новой заявки отработки оператора в активном цикле."""
        from .services.cycle import get_or_create_active_cycle
        from .services.otrabotka import get_disabled_days

        operator_id = request.query_params.get("operator_id")
        if not operator_id:
            return Response(
                {"detail": "operator_id обязателен"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        operator, err = self._resolve_otrabotka_operator(request, operator_id)
        if err:
            return err

        cycle = get_or_create_active_cycle()
        return Response({
            "operator_id": operator.id,
            "cycle": {
                "start_date": str(cycle.start_date),
                "end_date": str(cycle.end_date),
            },
            "disabled_days": get_disabled_days(operator, cycle),
        })

    @action(detail=False, methods=["post"], url_path="otrabotka/preview")
    def otrabotka_preview(self, request):
        """Сухой прогон: конфликты + распределение по дням (без записи в БД)."""
        from .services.otrabotka import OtrabotkaError, preview_otrabotka

        serializer = OtrabotkaAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        operator, err = self._resolve_otrabotka_operator(request, data["operator_id"])
        if err:
            return err

        try:
            result = preview_otrabotka(
                operator=operator,
                debt_detail_ids=data["debt_detail_ids"],
                days=data["days"],
            )
        except OtrabotkaError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code, **exc.payload},
                status=exc.http_status,
            )
        return Response(result)

    @action(detail=False, methods=["post"], url_path="otrabotka")
    def otrabotka_accept(self, request):
        """Принять заявку отработки (после подтверждения ha/yo'q)."""
        from .services.otrabotka import OtrabotkaError, accept_otrabotka

        serializer = OtrabotkaAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        operator, err = self._resolve_otrabotka_operator(request, data["operator_id"])
        if err:
            return err

        source = "requested" if request.user.role == "operator" else "manual"
        try:
            comp = accept_otrabotka(
                operator=operator,
                debt_detail_ids=data["debt_detail_ids"],
                days=data["days"],
                comment=data.get("comment", ""),
                user=request.user,
                source=source,
                pdf_file=data.get("pdf_file"),
                screens=data.get("screens"),
            )
        except OtrabotkaError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code, **exc.payload},
                status=exc.http_status,
            )

        return Response(
            CompensationDetailSerializer(comp, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="otrabotka/retroactive")
    def otrabotka_retroactive(self, request):
        """
        Ретроактивная отработка: оператор УЖЕ отработал долг в прошедшие дни
        цикла. Заявка принимается и СРАЗУ проверяется по факту (verify) —
        статус approved/partial/declined проставляется на месте.
        (Обычная проверка otrabotka — автоматическая, в дневном пайплайне.)
        """
        from .services.otrabotka import OtrabotkaError, accept_otrabotka

        serializer = OtrabotkaAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        operator, err = self._resolve_otrabotka_operator(request, data["operator_id"])
        if err:
            return err

        try:
            comp = accept_otrabotka(
                operator=operator,
                debt_detail_ids=data["debt_detail_ids"],
                days=data["days"],
                comment=data.get("comment", ""),
                user=request.user,
                pdf_file=data.get("pdf_file"),
                screens=data.get("screens"),
                retroactive=True,
            )
        except OtrabotkaError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code, **exc.payload},
                status=exc.http_status,
            )

        return Response(
            CompensationDetailSerializer(comp, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    # ------------------------------------------------------------------ #
    # Прямое списание долга (любой тип compensation, КРОМЕ otrabotka)
    # ------------------------------------------------------------------ #

    @action(detail=False, methods=["post"], url_path="debt/preview")
    def debt_comp_preview(self, request):
        """Сухой прогон списания: сумма долгов, списываемая сумма, остаток."""
        from .services.debt_compensation import (
            DebtCompensationError, preview_debt_compensation,
        )

        serializer = DebtCompensationAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        operator, err = self._resolve_otrabotka_operator(request, data["operator_id"])
        if err:
            return err

        try:
            result = preview_debt_compensation(
                operator=operator,
                code=data["code"],
                debt_detail_ids=data["debt_detail_ids"],
                applied_duration=data.get("applied_duration"),
            )
        except DebtCompensationError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code, **exc.payload},
                status=exc.http_status,
            )
        return Response(result)

    @action(detail=False, methods=["post"], url_path="debt")
    def debt_comp_accept(self, request):
        """Принять заявку-оправдание: списать долг, перенести записи долга в заявку."""
        from .services.debt_compensation import (
            DebtCompensationError, accept_debt_compensation,
        )

        serializer = DebtCompensationAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        operator, err = self._resolve_otrabotka_operator(request, data["operator_id"])
        if err:
            return err

        source = "requested" if request.user.role == "operator" else "manual"
        try:
            comp = accept_debt_compensation(
                operator=operator,
                code=data["code"],
                debt_detail_ids=data["debt_detail_ids"],
                applied_duration=data.get("applied_duration"),
                comment=data.get("comment", ""),
                user=request.user,
                source=source,
                pdf_file=data.get("pdf_file"),
                screens=data.get("screens"),
            )
        except DebtCompensationError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code, **exc.payload},
                status=exc.http_status,
            )

        return Response(
            CompensationDetailSerializer(comp, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


# =============================================================================
# Ретроактивная проверка
# =============================================================================

class RetroactiveCompensationCheckView(APIView):
    """
    Ретроактивная проверка: оператор уже отработал, теперь подаёт заявку.
    Создаёт Compensation с source='retroactive' и запускает авто-проверку.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = RetroactiveCheckRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            operator = Operator.objects.get(id=data["operator_id"])
            rule = RequestTypeRule.objects.get(
                code=data["type_rule_code"],
                category="compensation",
                is_active=True,
            )
        except (Operator.DoesNotExist, RequestTypeRule.DoesNotExist):
            return Response(
                {"detail": "Оператор или тип не найден"},
                status=status.HTTP_404_NOT_FOUND,
            )

        from .services.cycle import get_or_create_active_cycle
        from .services.retroactive_check import auto_check_retroactive

        cycle = get_or_create_active_cycle()

        # Проверка прав
        if request.user.role == "operator":
            op = getattr(request.user, "operator", None)
            if not op or op.id != operator.id:
                return Response(
                    {"detail": "Нельзя создавать заявку для другого оператора"},
                    status=status.HTTP_403_FORBIDDEN,
                )

        comp = Compensation(
            operator=operator,
            cycle=cycle,
            type_rule=rule,
            source="retroactive",
            planned_date=data["planned_date"],
            requested_duration=data["requested_duration"],
            comment=data.get("reason_text", ""),
            claim_metadata={
                "reason_text": data.get("reason_text", ""),
                "witnesses": data.get("witnesses", ""),
                "submitted_by_user_id": request.user.id,
            },
            fixed_by=request.user,
            status="pending",
        )

        try:
            comp.full_clean()
            comp.save()
        except Exception as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Запуск авто-проверки
        auto_check_retroactive(comp, data["planned_date"], cycle)

        # Возвращаем результат
        comp.refresh_from_db()
        return Response(
            CompensationDetailSerializer(comp, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


# =============================================================================
# Transfer
# =============================================================================

class TransferViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    parser_classes = (JSONParser, MultiPartParser, FormParser)
    serializer_class = TransferSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = TransferFilter
    search_fields = [
        "operator__surname", "operator__name", "operator__login_id",
        "comment",
    ]
    ordering_fields = ["date_from", "date_to", "created_at"]
    pagination_class = TransferPagination

    def get_queryset(self):
        user = self.request.user
        qs = Transfer.objects.select_related(
            "operator", "operator__group", "type_rule", "cycle", "fixed_by",
        )

        if user.role == "operator":
            op = getattr(user, "operator", None)
            qs = qs.filter(operator=op) if op else qs.none()
        elif user.role == "supervisor":
            group_ids = user.supervised_groups.values_list("id", flat=True)
            qs = qs.filter(operator__group_id__in=group_ids)

        return qs.order_by("-created_at")

    def perform_create(self, serializer):
        # ЕДИНАЯ матрица конфликтов (services.conflicts) — для generic-приёма
        # (перенос рабочего дня и пр.): нельзя на пересекающиеся дни с
        # конфликтующей заявкой.
        from datetime import timedelta
        from rest_framework.exceptions import ValidationError
        from .services.conflicts import CONFLICTS, find_conflicts

        data = serializer.validated_data
        rule = data.get("type_rule")
        operator = data.get("operator")
        code = rule.code if rule else None
        df = data.get("date_from")
        dt = data.get("date_to") or df
        if code in CONFLICTS and CONFLICTS[code] and operator and df:
            days = [df + timedelta(days=i) for i in range((dt - df).days + 1)]
            conf = find_conflicts(operator, code, days)
            if conf:
                raise ValidationError({"conflict": (
                    "Конфликт с заявками: "
                    + "; ".join(f"{c['display_name']} ({c['day']})" for c in conf[:10])
                )})
        serializer.save()

    # ------------------------------------------------------------------ #
    # Отпрашивание (otprashivanie) — приём заявки (Transfer = отгул,
    # связанный Compensation(otrabotka) = отработка)
    # ------------------------------------------------------------------ #

    def _resolve_operators(self, request, operator_ids):
        """Найти операторов с проверкой прав. Возвращает (operators, error_response)."""
        operators = list(Operator.objects.filter(id__in=operator_ids))
        found = {o.id for o in operators}
        missing = [i for i in operator_ids if i not in found]
        if missing:
            return None, Response(
                {"detail": f"Операторы не найдены: {missing}"},
                status=status.HTTP_404_NOT_FOUND)

        user = request.user
        if user.role == "operator":
            op = getattr(user, "operator", None)
            if not op or any(o.id != op.id for o in operators):
                return None, Response(
                    {"detail": "Нельзя подавать заявку за другого оператора"},
                    status=status.HTTP_403_FORBIDDEN)
        elif user.role == "supervisor":
            group_ids = set(user.supervised_groups.values_list("id", flat=True))
            outside = [o.id for o in operators if o.group_id not in group_ids]
            if outside:
                return None, Response(
                    {"detail": f"Операторы вне ваших групп: {outside}"},
                    status=status.HTTP_403_FORBIDDEN)
        return operators, None

    @action(detail=False, methods=["post"], url_path="otprashivanie/preview")
    def otprashivanie_preview(self, request):
        """Сухой прогон: длительности, распределение, конфликты по операторам."""
        from .services.otprashivanie import (
            OtprashivanieError, preview_otprashivanie,
        )

        serializer = OtprashivanieAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        operators, err = self._resolve_operators(request, data["operator_ids"])
        if err:
            return err
        try:
            result = preview_otprashivanie(
                operators=operators,
                leave_days=data["leave_days"],
                hour_from=data["hour_from"],
                hour_to=data["hour_to"],
                workoff_days=data["workoff_days"],
            )
        except OtprashivanieError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code, **exc.payload},
                status=exc.http_status)
        return Response(result)

    @action(detail=False, methods=["post"], url_path="otprashivanie")
    def otprashivanie_accept(self, request):
        """Принять заявку(и) отпрашивания + создать план отработки."""
        from .services.otprashivanie import (
            OtprashivanieError, accept_otprashivanie,
        )

        serializer = OtprashivanieAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        operators, err = self._resolve_operators(request, data["operator_ids"])
        if err:
            return err

        source = "requested" if request.user.role == "operator" else "manual"
        try:
            result = accept_otprashivanie(
                operators=operators,
                leave_days=data["leave_days"],
                hour_from=data["hour_from"],
                hour_to=data["hour_to"],
                workoff_days=data["workoff_days"],
                comment=data.get("comment", ""),
                user=request.user,
                source=source,
                pdf_file=data.get("pdf_file"),
                screens=data.get("screens"),
            )
        except OtprashivanieError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code, **exc.payload},
                status=exc.http_status)
        return Response(result, status=status.HTTP_201_CREATED)

    # ------------------------------------------------------------------ #
    # Учёба в рабочее время (ucheba_rabochee)
    # ------------------------------------------------------------------ #

    @action(detail=False, methods=["post"], url_path="ucheba/preview")
    def ucheba_preview(self, request):
        """Сухой прогон учёбы: окно, операторы + причины отбрасывания дней."""
        from .services.ucheba import UchebaError, preview_ucheba

        serializer = UchebaAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        operators, err = self._resolve_operators(request, data["operator_ids"])
        if err:
            return err
        try:
            result = preview_ucheba(
                operators=operators, days=data["days"],
                hour_from=data["hour_from"], hour_to=data["hour_to"])
        except UchebaError as exc:
            return Response({"detail": exc.message, "code": exc.code, **exc.payload},
                            status=exc.http_status)
        return Response(result)

    @action(detail=False, methods=["post"], url_path="ucheba")
    def ucheba_accept(self, request):
        """Принять заявку учёбы (status=pending, проверка по факту в пайплайне)."""
        from .services.ucheba import UchebaError, accept_ucheba

        serializer = UchebaAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        operators, err = self._resolve_operators(request, data["operator_ids"])
        if err:
            return err
        source = "requested" if request.user.role == "operator" else "manual"
        try:
            result = accept_ucheba(
                operators=operators, days=data["days"],
                hour_from=data["hour_from"], hour_to=data["hour_to"],
                comment=data.get("comment", ""), user=request.user, source=source,
                pdf_file=data.get("pdf_file"), screens=data.get("screens"))
        except UchebaError as exc:
            return Response({"detail": exc.message, "code": exc.code, **exc.payload},
                            status=exc.http_status)
        return Response(result, status=status.HTTP_201_CREATED)

    # ------------------------------------------------------------------ #
    # Льготы / освобождения (Исключение, Обучение, Льготы, Хоз.работы)
    # ------------------------------------------------------------------ #

    def _resolve_benefit_operators(self, request, data):
        """Операторы для льготы: явный список ИЛИ вся 9ч/12ч смена (с правами)."""
        from .services.benefit import resolve_operators

        if data.get("select_all_shift"):
            if request.user.role == "operator":
                return None, Response(
                    {"detail": "Оператор не может выбрать всю смену"},
                    status=status.HTTP_403_FORBIDDEN)
            ops = resolve_operators(None, data["select_all_shift"], data["days"])
            if request.user.role == "supervisor":
                gids = set(request.user.supervised_groups.values_list("id", flat=True))
                ops = [o for o in ops if o.group_id in gids]
            return ops, None
        return self._resolve_operators(request, data.get("operator_ids") or [])

    @action(detail=False, methods=["post"], url_path="benefit/preview")
    def benefit_preview(self, request):
        """Сухой прогон льготы: тип, полный день/часы, операторы + причины."""
        from .services.benefit import BenefitError, preview_benefit

        serializer = BenefitAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        operators, err = self._resolve_benefit_operators(request, data)
        if err:
            return err
        try:
            result = preview_benefit(
                operators=operators, code=data["code"],
                subtype=data.get("subtype"), days=data["days"],
                hour_from=data.get("hour_from"), hour_to=data.get("hour_to"))
        except BenefitError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code, **exc.payload},
                status=exc.http_status)
        return Response(result)

    @action(detail=False, methods=["post"], url_path="benefit")
    def benefit_accept(self, request):
        """Принять заявку-льготу (рабочий день отменяется, долг не начисляется)."""
        from .services.benefit import BenefitError, accept_benefit

        serializer = BenefitAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        operators, err = self._resolve_benefit_operators(request, data)
        if err:
            return err

        source = "requested" if request.user.role == "operator" else "manual"
        try:
            result = accept_benefit(
                operators=operators, code=data["code"],
                subtype=data.get("subtype"), days=data["days"],
                hour_from=data.get("hour_from"), hour_to=data.get("hour_to"),
                comment=data.get("comment", ""), user=request.user,
                source=source, pdf_file=data.get("pdf_file"),
                screens=data.get("screens"))
        except BenefitError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code, **exc.payload},
                status=exc.http_status)
        return Response(result, status=status.HTTP_201_CREATED)


# =============================================================================
# ManualAdjustment
# =============================================================================

class ManualAdjustmentViewSet(viewsets.ModelViewSet):
    serializer_class = ManualAdjustmentSerializer
    permission_classes = [CanManualAdjust]
    filter_backends = [DjangoFilterBackend]
    filterset_class = ManualAdjustmentFilter
    pagination_class = StandardPagination

    def get_queryset(self):
        user = self.request.user
        qs = ManualAdjustment.objects.select_related(
            "operator", "adjusted_by", "approved_by",
        )

        if user.role == "supervisor":
            op = getattr(user, "operator", None)
            if op:
                group_ids = user.supervised_groups.values_list("id", flat=True)
                qs = qs.filter(operator__group_id__in=group_ids)
            else:
                qs = qs.none()

        return qs.order_by("-adjusted_at")

    def perform_create(self, serializer):
        serializer.save(adjusted_by=self.request.user)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        from .services.manual_adjustment import approve_adjustment

        adjustment = self.get_object()
        try:
            approve_adjustment(adjustment, request.user)
            return Response({"status": "approved"})
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )


# =============================================================================
# AutomationOverride
# =============================================================================

class AutomationOverrideViewSet(viewsets.ModelViewSet):
    queryset = AutomationOverride.objects.select_related(
        "operator", "created_by", "deactivated_by",
    )
    serializer_class = AutomationOverrideSerializer
    permission_classes = [IsManagerOrAdmin]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["operator", "override_type", "is_active"]
    pagination_class = StandardPagination

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate(self, request, pk=None):
        override = self.get_object()
        override.is_active = False
        override.deactivated_at = now()
        override.deactivated_by = request.user
        override.save(update_fields=[
            "is_active", "deactivated_at", "deactivated_by",
        ])
        return Response({"status": "deactivated"})


# =============================================================================
# Note / EventLog / Logos
# =============================================================================

class NoteViewSet(viewsets.ModelViewSet):
    serializer_class = NoteSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["target_type", "target_id", "visibility"]
    pagination_class = StandardPagination

    def get_queryset(self):
        user = self.request.user
        qs = Note.objects.select_related("created_by").order_by("-created_at")

        # Фильтрация по видимости
        if user.role == "operator":
            qs = qs.filter(visibility__in=["public", "operator"])
        elif user.role == "supervisor":
            qs = qs.filter(visibility__in=["public", "operator", "supervisor"])

        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class EventLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = EventLog.objects.all().select_related("operator", "triggered_by")
    serializer_class = EventLogSerializer
    permission_classes = [IsManagerOrAdmin]
    filter_backends = [DjangoFilterBackend]
    filterset_class = EventLogFilter
    pagination_class = LargePagination
    ordering = ["-timestamp"]


def _as_bool(value) -> bool:
    """Разбор булева из query/body ('1','true','yes' -> True)."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class RunDailyPipelineView(APIView):
    """Ручной запуск ежедневного конвейера (daily_runner.run) через Celery.

    Дёргать можно ПРОСТО ИЗ БРАУЗЕРА (GET), передав секрет в query:
      GET /api/v1/run-daily-pipeline/?token=<SYNC_SERVICE_TOKEN>
      GET /api/v1/run-daily-pipeline/?token=<...>&date=YYYY-MM-DD&skip_sheets=1

    Или как обычно (POST + JWT admin/manager):
      POST /api/v1/run-daily-pipeline/  {"date": "...", "skip_sheets": false}

    По умолчанию — за вчера. Задача уходит в Celery-воркер (не блокирует HTTP),
    логи — в логах воркера. Возвращает id задачи.
    """
    permission_classes = [permissions.AllowAny]  # доступ по ?token= ИЛИ admin/manager

    def _authorized(self, request) -> bool:
        # 1) секрет в query — чтобы дёргать прямо из адресной строки браузера
        token = request.query_params.get("token")
        if token and token == settings.SYNC_SERVICE_TOKEN:
            return True
        # 2) или залогиненный admin/manager
        u = request.user
        return bool(
            u and u.is_authenticated
            and getattr(u, "role", None) in ("admin", "manager")
        )

    def _run(self, request):
        if not self._authorized(request):
            return Response(
                {"detail": "Ruxsat yo'q. Kerak: ?token=<SYNC_SERVICE_TOKEN> yoki admin/manager JWT."},
                status=status.HTTP_403_FORBIDDEN,
            )

        raw_date = request.query_params.get("date") or request.data.get("date")
        target_iso = None
        if raw_date:
            try:
                target_iso = date.fromisoformat(str(raw_date)).isoformat()
            except ValueError:
                return Response(
                    {"detail": "Noto'g'ri date format (kerak: YYYY-MM-DD)"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        skip_sheets = _as_bool(
            request.query_params.get("skip_sheets", request.data.get("skip_sheets", False))
        )

        from .tasks import daily_pipeline_task
        async_result = daily_pipeline_task.delay(
            target_date_iso=target_iso,
            skip_sheets=skip_sheets,
        )
        return Response(
            {
                "detail": "Daily pipeline ishga tushirildi (Celery)",
                "task_id": async_result.id,
                "target_date": target_iso or "kecha (default)",
                "skip_sheets": skip_sheets,
            },
            status=status.HTTP_202_ACCEPTED,
        )

    def get(self, request):
        return self._run(request)

    def post(self, request):
        return self._run(request)
