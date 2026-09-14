"""
Загрузка ежедневных логов с внешнего API в WorkLogDaily.
"""

import logging
from datetime import date, timedelta
from typing import Optional

from django.db import transaction

from hourly_locks.models import (
    Compensation,
    CompensationDay,
    Operator,
    OperatorScheduleDay,
    Shift,
    UchebaDay,
    WorkLogDaily,
)

from . import external_api
from . import shift as shift_service
from .automation_override import has_override
from .cycle import get_cycle_for_date, get_or_create_active_cycle
from .event_log import log_event

logger = logging.getLogger(__name__)


# =============================================================================
# Основная функция: загрузка логов за день для всех операторов
# =============================================================================

def load_logs_for_day(target_date: date) -> dict:
    """
    Загружает ежедневные логи для всех операторов за указанный день.

    Алгоритм:
      1. Получаем смену для каждого оператора (OperatorScheduleDay).
      2. По смене определяем окно часов для запроса.
      3. Запрашиваем API почасовые данные.
      4. Агрегируем по операторам.
      5. Сохраняем в WorkLogDaily.

    Возвращает статистику.
    """
    cycle = get_cycle_for_date(target_date) or get_or_create_active_cycle()

    if not cycle.contains(target_date):
        logger.warning(
            "Дата %s не в активном цикле %s. Пропуск.",
            target_date, cycle,
        )
        return {"status": "skipped", "reason": "date_outside_active_cycle"}

    next_date = target_date + timedelta(days=1)

    # Загружаем «широкое окно» — обычные смены + первая половина следующего дня
    logger.info("[log_loader] Запрос API за %s (24 часа)...", target_date)
    day_data = external_api.fetch_hours_range(target_date, range(24))

    logger.info("[log_loader] Запрос API за %s (0-10 часов)...", next_date)
    next_day_data = external_api.fetch_hours_range(next_date, range(10))

    # Группируем по оператору
    stats = {
        "loaded": 0,
        "skipped_no_shift": 0,
        "skipped_override": 0,
        "skipped_20_08_with_comp": 0,
        "errors": 0,
    }

    # Получаем расписание всех операторов на этот день.
    # Исключённые группы (deny-list) пропускаем: логи не грузим → и долг за них
    # не считается (WorkLogDaily не создаётся).
    from .group_scope import exclude_operators
    schedules = exclude_operators(
        OperatorScheduleDay.objects
        .filter(day=target_date)
        .select_related("operator", "shift"),
        path="operator",
    )

    for schedule_day in schedules:
        operator = schedule_day.operator

        try:
            result = _load_single_operator(
                operator=operator,
                schedule_day=schedule_day,
                target_date=target_date,
                next_date=next_date,
                day_data=day_data,
                next_day_data=next_day_data,
                cycle=cycle,
            )
            stats[result] = stats.get(result, 0) + 1

        except Exception as exc:
            logger.exception(
                "[log_loader] Ошибка для %s: %s", operator, exc,
            )
            log_event(
                event_type="log_load_failed",
                level="error",
                operator=operator,
                message=f"Ошибка загрузки лога: {exc}",
                payload={"date": str(target_date)},
            )
            stats["errors"] += 1

    log_event(
        event_type="log_loaded",
        level="info",
        message=f"Загрузка логов за {target_date} завершена",
        cycle=cycle,
        payload=stats,
    )

    logger.info("[log_loader] Загрузка за %s завершена: %s", target_date, stats)
    return stats


def _load_single_operator(
    operator: Operator,
    schedule_day: OperatorScheduleDay,
    target_date: date,
    next_date: date,
    day_data: dict,
    next_day_data: dict,
    cycle,
) -> str:
    """
    Загружает лог для одного оператора. Возвращает код результата.
    """
    # Переопределение автоматики
    if has_override(operator, "skip_log_load", on_date=target_date):
        logger.info("[log_loader] SKIP %s — skip_log_load", operator)
        return "skipped_override"

    # Выходной — пропускаем
    if schedule_day.is_day_off or not schedule_day.shift:
        return "skipped_no_shift"

    shift_obj = schedule_day.shift

    # 20-08 со СТАРОЙ компенсацией (verification_strategy=night_pipeline) —
    # обрабатывается ночным конвейером. Отработка (schedule_based) НЕ
    # пропускается: ей нужен WorkLogDaily с 24ч-плиткой (см. ниже).
    if shift_obj.requires_special_pipeline:
        has_night_comp = Compensation.objects.filter(
            operator=operator,
            planned_date=target_date,
            status__in=["pending", "approved", "partial"],
            type_rule__verification_strategy="night_pipeline",
        ).exists()

        if has_night_comp:
            logger.info(
                "[log_loader] SKIP %s — 20-08 с компенсацией обрабатывается ночным конвейером",
                operator,
            )
            return "skipped_20_08_with_comp"

    # Определяем окно часов. Широкое (24ч-плитка) окно нужно, если на этот день
    # есть отработка — по planned_date (старые компенсации) ИЛИ по дню отработки
    # (CompensationDay, каждый день многодневной/последовательной отработки).
    has_pending_comp = (
        Compensation.objects.filter(
            operator=operator, planned_date=target_date, status="pending",
        ).exists()
        or CompensationDay.objects.filter(
            compensation__operator=operator, day=target_date,
            compensation__type_rule__code="otrabotka",
            compensation__status__in=["pending", "partial"],
        ).exists()
    )

    # Учёба в рабочее время: РАСШИРЕННОЕ окно (от начала смены до 08:00 след.
    # дня), чтобы захватить доработку после возвращения с учёбы. Часы с full=0
    # (окно учёбы и хвост) в сумму не войдут.
    has_ucheba = UchebaDay.objects.filter(
        transfer__operator=operator, day=target_date,
        transfer__type_rule__code="ucheba_rabochee",
        transfer__status__in=["pending", "approved", "partial"],
    ).exists()

    if has_ucheba:
        start_h, end_h, crosses = shift_obj.start_time.hour, 7, True
    elif has_pending_comp:
        SPECIAL_CODES = {"17-02", "18-03", "15-24", "20-08"}
        if shift_obj.code in SPECIAL_CODES:
            start_h, end_h, crosses = shift_service.get_fetch_window_for_special(
                shift_obj.code,
            )
        else:
            start_h, end_h, crosses = 6, 23, False
    else:
        start_h, end_h, crosses = shift_service.get_fetch_window(shift_obj)

    # Собираем часы оператора
    sums = {
        "aftercall_duration": timedelta(),
        "busy_duration": timedelta(),
        "hold_duration": timedelta(),
        "idle_duration": timedelta(),
        "lazy_duration": timedelta(),
        "lock_duration": timedelta(),
        "relax_duration": timedelta(),
        "full_duration": timedelta(),
    }

    if not crosses:
        # Обычная смена: только в пределах target_date
        for hour in range(start_h, end_h + 1):
            rows = day_data.get(hour, [])
            _accumulate_for_login(sums, rows, operator.login_id)
    else:
        # Ночная: target_date с start_h до 23 + next_date с 0 до end_h
        for hour in range(start_h, 24):
            rows = day_data.get(hour, [])
            _accumulate_for_login(sums, rows, operator.login_id)
        for hour in range(0, end_h + 1):
            rows = next_day_data.get(hour, [])
            _accumulate_for_login(sums, rows, operator.login_id)

    # Сохраняем
    with transaction.atomic():
        start_at, end_at = shift_service.calculate_shift_datetime_bounds(
            shift_obj, target_date,
        )

        WorkLogDaily.objects.update_or_create(
            operator=operator,
            day=target_date,
            defaults={
                "cycle": cycle,
                "shift": shift_obj,
                "shift_code_snapshot": shift_obj.code,
                "start_at": start_at,
                "end_at": end_at,
                **sums,
            },
        )

    return "loaded"


def _accumulate_for_login(sums: dict, rows: list, login_id: str):
    """Суммирует длительности из rows для конкретного login_id."""
    delta = external_api.sum_for_login(rows, login_id)
    for key, value in delta.items():
        sums[key] += value