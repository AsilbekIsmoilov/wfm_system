"""
Расчёт долгов операторов на основе ежедневных логов.

Алгоритм:
  1. Получаем WorkLogDaily для оператора за день.
  2. По смене получаем нормы (norm_full, norm_lock, tolerance).
  3. Считаем недоработку и превышение перерыва.
  4. Учитываем компенсирующую переработку.
  5. Создаём WorkDebtDetail и обновляем WorkDebt.
"""

import logging
from datetime import date, timedelta
from typing import Dict, Optional

from django.db import transaction

from hourly_locks.models import (
    Compensation,
    Operator,
    OperatorScheduleDay,
    RequestTypeRule,
    Shift,
    Transfer,
    WorkDebt,
    WorkDebtDetail,
    WorkLogDaily,
)

from .automation_override import get_skipped_operator_ids
from .cycle import get_or_create_active_cycle
from .event_log import log_event

logger = logging.getLogger(__name__)


# =============================================================================
# Проверка одного лога (бывший check_debt)
# =============================================================================

def _hms(delta) -> str:
    """Длительность H:MM:SS (с минусом для отрицательных)."""
    if delta is None:
        delta = timedelta(0)
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    return f"{sign}{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def check_debt_for_log(log: WorkLogDaily) -> Dict:
    """
    Проверяет один WorkLogDaily на наличие долга по правилам смены.

    Возвращает dict с:
      - violation_type
      - debt_full, debt_lock
      - norm_full, norm_lock
      - make_up_wh (компенсирующая переработка)
      - note (текстовое описание)
    """
    shift = log.shift
    full = log.full_duration or timedelta()
    lock = log.lock_duration or timedelta()
    Z = timedelta(0)

    if not shift:
        return _empty_result(note=f"Неизвестная смена для {log.day}")

    norm_full = shift.norm_full or Z          # полная смена (9:00 / 8:00 / 12:00)
    tolerance = shift.tolerance_undertime or Z

    # Норма перерыва и порог (grace):
    #   9h/8h: норма = warn_at (1:30), мягкий порог = soft_cap (1:45)
    #   12h:   норма = soft_cap (2:20), grace = 0
    if shift.norm_lock_warn_at and shift.norm_lock_warn_at > Z:
        lock_norm = shift.norm_lock_warn_at
        soft_cap = shift.norm_lock_soft_cap or lock_norm
    else:
        lock_norm = shift.norm_lock_soft_cap or Z
        soft_cap = lock_norm
    grace = max(soft_cap - lock_norm, Z)

    work_norm = max(norm_full - lock_norm, Z)  # норма рабочих часов (7:30 / 6:30 / 9:40)
    work_hours = full - lock                   # фактические рабочие часы

    # ----- Общий долг по рабочим часам (с послаблением перерыва) -----
    work_shortfall = max(work_norm - work_hours, Z)
    lock_over = max(lock - lock_norm, Z)
    forgiven = min(lock_over, grace) if lock <= soft_cap else Z
    total = max(work_shortfall - forgiven, Z)

    # ----- Разбивка: присутствие (недоработка) vs перерыв (штраф) -----
    presence_deficit = max(norm_full - full, Z)   # опоздание / ранний уход / неявка
    debt_full = min(total, presence_deficit)
    debt_lock = total - debt_full

    # ----- Допуск недоработки (только для смены) — порог на часть присутствия -----
    if tolerance > Z and Z < debt_full <= tolerance:
        debt_full = Z

    make_up_wh = max(full - norm_full, Z)         # переработка (доделал), компенсирует перерыв

    has_insufficient_wh = debt_full > Z
    has_exceeding_break = debt_lock > Z
    if has_insufficient_wh and has_exceeding_break:
        violation_type = "both_violations"
    elif has_insufficient_wh:
        violation_type = "insufficient_wh"
    elif has_exceeding_break:
        violation_type = "exceeding_break"
    else:
        violation_type = None

    note = (
        f"Рабочие часы (факт−перерыв): {_hms(work_hours)} при норме {_hms(work_norm)}.\n"
        f"Перерыв: факт {_hms(lock)}, норма {_hms(lock_norm)}, порог {_hms(soft_cap)}"
        + (f", послабление {_hms(forgiven)}" if forgiven > Z else "")
        + (f", переработка {_hms(make_up_wh)}" if make_up_wh > Z else "")
        + f".\nДолг: недоработка {_hms(debt_full)}, перерыв {_hms(debt_lock)}."
    )

    return {
        "debt_full": debt_full,
        "debt_lock": debt_lock,
        "norm_full": work_norm,
        "norm_lock": lock_norm,
        "violation_type": violation_type,
        "make_up_wh": make_up_wh,
        "note": note,
    }


def _empty_result(note: str = "") -> Dict:
    return {
        "debt_full": timedelta(),
        "debt_lock": timedelta(),
        "norm_full": timedelta(),
        "norm_lock": timedelta(),
        "violation_type": None,
        "make_up_wh": timedelta(),
        "note": note,
    }


# =============================================================================
# Пакетный расчёт долгов за день
# =============================================================================

def calculate_work_debts(for_date: date) -> dict:
    """
    Считает долги для всех операторов за указанный день.

    Логика:
      1. Пропускаем операторов с AutomationOverride='skip_debt_calc'.
      2. Пропускаем операторов с активным Transfer типа, освобождающего от долга
         (RequestTypeRule.exempts_from_daily_debt=True).
      3. Пропускаем 20-08 с компенсацией (обрабатывается ночным конвейером).
      4. Для остальных — check_debt_for_log → создание WorkDebtDetail.
    """
    # Pipeline ichida WDD create/update/delete ko'p bo'ladi; funksiya oxirida
    # recompute_for_date baribir WorkDebt'ni BIR MARTA yangilaydi. Shuning uchun
    # WDD-delete signal-recompute'ni o'chiramiz (takroriy hisobni oldini olish).
    # Signal esa faqat QO'LDA (pipeline'dan tashqari) o'chirish uchun qoladi.
    from hourly_locks.signals import skip_wdd_delete_recompute
    skip_wdd_delete_recompute(True)
    try:
        return _calculate_work_debts_inner(for_date)
    finally:
        skip_wdd_delete_recompute(False)


def _calculate_work_debts_inner(for_date: date) -> dict:
    cycle = get_or_create_active_cycle()

    if not cycle.contains(for_date):
        logger.info("[debt_calc] %s вне активного цикла, пропуск", for_date)
        return {"status": "skipped", "reason": "date_outside_cycle"}

    logger.info("[debt_calc] Старт расчёта за %s", for_date)

    # Операторы с переопределением — пропускаем
    skipped_override_ids = get_skipped_operator_ids("skip_debt_calc", for_date)

    # Операторы с активными Transfer'ами, освобождающими от долга
    exempt_rules = RequestTypeRule.objects.filter(
        category="transfer",
        exempts_from_daily_debt=True,
        is_active=True,
    )
    EXEMPT_STATUSES = ["pending", "in_progress", "approved"]

    exempt_full_ids = set()  # operator_id, полностью освобождённые
    exempt_partial = {}  # operator_id → {"duration": ..., "type_code": ...}

    def _add_partial(op_id, dur, code):
        prev = exempt_partial.get(op_id, {}).get("duration", timedelta(0))
        exempt_partial[op_id] = {"duration": prev + dur, "type_code": code}

    # (1) Льготы по-дневно (BenefitDay): одна заявка покрывает несколько
    #     (в т.ч. непоследовательных) дней — освобождаем именно эти дни.
    from hourly_locks.models import BenefitDay
    for bd in BenefitDay.objects.filter(
        day=for_date,
        transfer__type_rule__in=exempt_rules,
        transfer__status__in=EXEMPT_STATUSES,
    ).select_related("transfer", "transfer__type_rule"):
        op_id = bd.transfer.operator_id
        if bd.duration and bd.duration > timedelta(0):
            _add_partial(op_id, bd.duration, bd.transfer.type_rule.code)
        else:
            exempt_full_ids.add(op_id)  # весь день

    # (2) Прочие exempt-заявки по ДИАПАЗОНУ дат, у которых нет BenefitDay
    #     (напр. ucheba_rabochee, старые заявки) — прежняя логика.
    exempt_qs = Transfer.objects.filter(
        type_rule__in=exempt_rules,
        status__in=EXEMPT_STATUSES,
        date_from__lte=for_date,
        date_to__gte=for_date,
        benefit_days__isnull=True,
    ).select_related("type_rule").distinct()

    for transfer in exempt_qs:
        op_id = transfer.operator_id
        if transfer.requested_duration and transfer.requested_duration > timedelta(0):
            _add_partial(op_id, transfer.requested_duration, transfer.type_rule.code)
        else:
            exempt_full_ids.add(op_id)

    # Переносы (тип transfer) — на date_from пропускаем
    transfer_rule_codes = ["transfer"]
    transfer_ops_today = set(
        Transfer.objects.filter(
            type_rule__code__in=transfer_rule_codes,
            status__in=["pending", "in_progress"],
            date_from=for_date,
        ).values_list("operator_id", flat=True)
    )

    # 20-08 с компенсацией
    special_pipeline_ids = set(
        OperatorScheduleDay.objects.filter(
            day=for_date,
            shift__requires_special_pipeline=True,
        )
        .filter(
            operator_id__in=Compensation.objects.filter(
                planned_date=for_date,
                status__in=["pending", "approved", "partial"],
            ).values_list("operator_id", flat=True)
        )
        .values_list("operator_id", flat=True)
    )

    # Учёба в рабочее время: дневную норму считает services.ucheba (по
    # расширенному окну), поэтому обычный расчёт пропускаем.
    from hourly_locks.models import UchebaDay
    ucheba_op_ids = set(
        UchebaDay.objects.filter(
            day=for_date,
            transfer__type_rule__code="ucheba_rabochee",
            transfer__status__in=["pending", "approved", "partial"],
        ).values_list("transfer__operator_id", flat=True)
    )

    # Основной цикл
    logs = WorkLogDaily.objects.filter(day=for_date).select_related(
        "operator", "shift",
    )
    stats = {"created": 0, "skipped": 0}

    with transaction.atomic():
        for log in logs:
            operator = log.operator
            op_id = operator.id

            if op_id in skipped_override_ids:
                stats["skipped"] += 1
                continue

            if op_id in special_pipeline_ids:
                logger.info(
                    "[debt_calc] SKIP %s (20-08 + comp → ночной конвейер)",
                    operator,
                )
                stats["skipped"] += 1
                continue

            if op_id in ucheba_op_ids:
                # день учёбы — норму считает services.ucheba (расш. окно)
                stats["skipped"] += 1
                continue

            if op_id in exempt_full_ids:
                # Полное освобождение (льгота/отгул на весь день): убираем
                # существующий (не привязанный к компенсации) долг за день,
                # чтобы льгота, принятая ПОСЛЕ расчёта, тоже обнуляла долг.
                WorkDebtDetail.objects.filter(
                    operator=operator, day=log.day, source="shift",
                    locked_for_compensation=False,
                ).delete()
                stats["skipped"] += 1
                continue

            if op_id in transfer_ops_today:
                stats["skipped"] += 1
                continue

            # Частичное освобождение
            if op_id in exempt_partial:
                _handle_partial_exempt(
                    log=log,
                    operator=operator,
                    cycle=cycle,
                    skip_info=exempt_partial[op_id],
                )
                stats["created"] += 1
                continue

            # Обычный расчёт
            _create_debt_record(log, operator, cycle)
            stats["created"] += 1

    # WorkDebt'larni qayta hisoblash (single source of truth)
    from .work_debt import recompute_for_date
    recompute_stats = recompute_for_date(for_date)
    stats["recomputed"] = recompute_stats

    log_event(
        event_type="debt_calculated",
        level="info",
        message=f"Расчёт долгов за {for_date}: {stats}",
        cycle=cycle,
        payload=stats,
    )

    logger.info("[debt_calc] Завершено за %s: %s", for_date, stats)
    return stats


def _create_debt_record(log: WorkLogDaily, operator: Operator, cycle):
    """
    Создаёт WorkDebtDetail.

    QOIDA: WorkDebt'ga to'g'ridan-to'g'ri tegmaymiz —
    `services.work_debt.recompute_for_operator` orqali hisoblanadi.

    Idempotency: locked WDD bo'lsa tegmaymiz.
    """
    # 0. Tekshirish: locked WDD bormi? Bor bo'lsa tegmaymiz (idempotency)
    locked_exists = WorkDebtDetail.objects.filter(
        operator=operator,
        day=log.day,
        source="shift",
        shift=log.shift,
        locked_for_compensation=True,
    ).exists()
    if locked_exists:
        logger.info(
            "[debt_calc] SKIP %s %s — WDD allaqachon kompensatsiyaga bog'langan",
            operator, log.day,
        )
        return

    # 1. Yangi qarz hisoblash
    result = check_debt_for_log(log)
    debt_value = result["debt_full"] + result["debt_lock"]

    # 2a. Qarz yo'q → "bu kun qarzi" entity endi mavjud emas: eski (locked bo'lmagan)
    #     yozuvni o'chiramiz. Bu delete WFM'ga alohida sync qilinishi kerak (delete-sync).
    if debt_value <= timedelta(0):
        WorkDebtDetail.objects.filter(
            operator=operator, day=log.day, source="shift", shift=log.shift,
            locked_for_compensation=False,
        ).delete()
        return

    # 2b. update_or_create — external_id (identity) BARQAROR qoladi.
    #     Ilgari delete+create har run YANGI PK yasar edi → WFM upsert'da eski PK
    #     orphan bo'lib to'planardi (bir WDD → N dublikat). Endi o'sha yozuv yangilanadi.
    WorkDebtDetail.objects.update_or_create(
        operator=operator,
        day=log.day,
        source="shift",
        shift=log.shift,
        defaults={
            "cycle": cycle,
            "shift_code_snapshot": log.shift.code if log.shift else None,
            "violation_type": result["violation_type"],
            "norm_full": result["norm_full"],
            "fact_full": log.full_duration,
            "debt_full": result["debt_full"],
            "norm_lock": result["norm_lock"],
            "fact_lock": log.lock_duration,
            "debt_lock": result["debt_lock"],
            "make_up_wh": result["make_up_wh"],
            "note": result["note"],
        },
    )


def _handle_partial_exempt(
    log: WorkLogDaily,
    operator: Operator,
    cycle,
    skip_info: dict,
):
    """
    Qisman ozod qilingan operatorning kunlik WDD'sini yangilaydi.

    QOIDA: WorkDebt'ga tegmaymiz — recompute orqali hisoblanadi.
    """
    skip_duration = skip_info["duration"]
    type_code = skip_info["type_code"]

    # LOCKED tekshirish
    locked_exists = WorkDebtDetail.objects.filter(
        operator=operator, day=log.day, source="shift",
        locked_for_compensation=True,
    ).exists()
    if locked_exists:
        logger.info(
            "[debt_calc partial] SKIP %s %s — WDD locked",
            operator, log.day,
        )
        return

    result = check_debt_for_log(log)
    day_total_debt = result["debt_full"] + result["debt_lock"]

    remaining_debt = day_total_debt - skip_duration
    if remaining_debt < timedelta(0):
        remaining_debt = timedelta(0)

    note = (
        f"Частичное освобождение ({type_code}): длительность {skip_duration}.\n"
        f"Факт={log.full_duration}, перерыв={log.lock_duration}.\n"
        f"Дневной долг={day_total_debt}, остаток={remaining_debt}."
    )

    WorkDebtDetail.objects.update_or_create(
        operator=operator,
        day=log.day,
        source="shift",
        defaults={
            "cycle": cycle,
            "shift": log.shift,
            "shift_code_snapshot": log.shift.code if log.shift else None,
            "violation_type": result["violation_type"],
            "norm_full": result["norm_full"],
            "fact_full": log.full_duration or timedelta(),
            "debt_full": remaining_debt,
            "norm_lock": result["norm_lock"],
            "fact_lock": log.lock_duration or timedelta(),
            "debt_lock": timedelta(0),
            "make_up_wh": result["make_up_wh"],
            "note": note,
        },
    )
