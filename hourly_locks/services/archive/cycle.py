"""
Главный архиватор цикла: при закрытии цикла все связанные данные
переносятся в архивную БД, а в default БД остаются только данные
нового активного цикла.

ВАРИАНТ A: все долги полностью архивируются, новый цикл начинается с 0.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils.timezone import now

from hourly_locks.models import (
    Compensation,
    CompensationDebtLink,
    Cycle,
    EventLog,
    ManualAdjustment,
    Operator,
    Transfer,
    WorkDebt,
    WorkDebtDetail,
    WorkLogDaily,
)

from .compensation import archive_compensations_for_cycle
from .debt import archive_debts_for_cycle
from .transfer import archive_transfers_for_cycle

logger = logging.getLogger(__name__)


def archive_cycle_data(cycle: Cycle) -> dict:
    """
    Архивирует все данные цикла, затем удаляет их из default DB.

    Стадии:
      0. ВРЕМЕННО: pending Compensation → declined; pending/in_progress
         Transfer → перенос на новый цикл (не архивируются).
      1. Снапшот операторов (для исторических ссылок).
      2. Архивация Compensation.
      3. Архивация Transfer.
      4. Архивация WorkDebt / WorkDebtDetail.
      5. Архивация WorkLogDaily (опционально).
      6. Архивация ManualAdjustment.
      7. Архивация значимых EventLog.
      8. Очистка default DB (только относящееся к закрываемому циклу).
    """
    logger.info("[archive_cycle] Начало архивации цикла %s", cycle)

    stats = {}

    # 0. pending Comp → declined; pending/in_progress Transfer → перенос
    #    (ВАЖНО: до архивации, чтобы перенесённые Transfer не попали в архив).
    stats["repointed"] = _repoint_cross_cycle_requests(cycle)

    # 1. Снапшот операторов
    stats["operators_snapshot"] = _archive_operator_snapshots(cycle)

    # 2. Compensation
    stats["compensations"] = archive_compensations_for_cycle(cycle)

    # 3. Transfer
    stats["transfers"] = archive_transfers_for_cycle(cycle)

    # 4. WorkDebt / WorkDebtDetail
    stats["debts"] = archive_debts_for_cycle(cycle)

    # 5. WorkLogDaily
    stats["logs"] = _archive_work_logs(cycle)

    # 6. ManualAdjustment
    stats["manual_adjustments"] = _archive_manual_adjustments(cycle)

    # 7. EventLog (только warning+)
    stats["event_logs"] = _archive_event_logs(cycle)

    # 8. Очистка default DB
    _cleanup_default_db(cycle)

    logger.info(
        "[archive_cycle] Завершено для цикла %s. Статистика: %s",
        cycle, stats,
    )
    return stats


# =============================================================================
# Cross-cycle re-pointing
# =============================================================================

@transaction.atomic
def _repoint_cross_cycle_requests(cycle: Cycle) -> dict:
    """
    ВРЕМЕННОЕ решение при закрытии цикла:

      - Compensation со статусом «pending» → «declined». Затем (на стадии 2)
        они архивируются как declined и удаляются из default DB вместе с
        остальными. Carry-over для компенсаций НЕ делаем.

      - Transfer со статусом «pending» / «in_progress» НЕ архивируются: они
        переносятся (re-point) на новый цикл и продолжают жить. В архив уйдут
        только когда станут терминальными (completed и т.п.) — при закрытии
        следующего цикла. Остальные статусы Transfer (approved/partial/declined/
        completed) архивируются как обычно.

    Возвращает {compensations_declined: N, transfers_carried_over: N}.
    """
    from hourly_locks.services.cycle import get_or_create_active_cycle

    next_cycle_start = cycle.end_date + timedelta(days=1)
    next_cycle = get_or_create_active_cycle(today=next_cycle_start)

    # 1. Compensation: все pending → declined (carry-over не делаем)
    comp_declined = Compensation.objects.filter(
        cycle=cycle, status="pending",
    ).update(status="declined")
    if comp_declined:
        logger.info(
            "[archive_cycle] %d pending Compensation → declined (перед архивацией)",
            comp_declined,
        )

    # 2. Transfer: pending / in_progress → перенос на новый цикл (не архивируем)
    tr_qs = Transfer.objects.filter(
        cycle=cycle, status__in=["pending", "in_progress"],
    )
    tr_carried = tr_qs.count()
    if tr_carried:
        tr_qs.update(cycle=next_cycle)
        logger.info(
            "[archive_cycle] %d Transfer (pending/in_progress) перенесено на "
            "новый цикл #%d (архивируются после completed)",
            tr_carried, next_cycle.id,
        )

    return {
        "compensations_declined": comp_declined,
        "transfers_carried_over": tr_carried,
    }


# =============================================================================
# Снапшот операторов
# =============================================================================

def _archive_operator_snapshots(cycle: Cycle) -> int:
    """Сохраняет снапшот всех активных операторов на момент закрытия."""
    from archive.models import ArchiveOperatorSnapshot

    operators = Operator.objects.filter(is_active=True).select_related("group")
    count = 0

    snapshots = []
    for op in operators:
        snapshots.append(ArchiveOperatorSnapshot(
            archive_year=cycle.year,
            archive_month=cycle.month,
            operator_id=op.id,
            operator_login=op.login_id,
            surname=op.surname,
            name=op.name,
            middle_name=op.middle_name,
            group_id=op.group_id,
            group_name=op.group.name if op.group else None,
        ))

    if snapshots:
        ArchiveOperatorSnapshot.objects.using("archive").bulk_create(
            snapshots, batch_size=500, ignore_conflicts=True,
        )
        count = len(snapshots)

    return count


# =============================================================================
# WorkLogDaily
# =============================================================================

def _archive_work_logs(cycle: Cycle) -> int:
    """Архивирует WorkLogDaily за цикл."""
    from archive.models import ArchiveWorkLogDaily

    logs = WorkLogDaily.objects.filter(cycle=cycle).select_related(
        "operator", "operator__group",
    )

    rows = []
    for log in logs:
        op = log.operator
        rows.append(ArchiveWorkLogDaily(
            archive_year=cycle.year,
            archive_month=cycle.month,
            operator_id=op.id,
            operator_login=op.login_id,
            operator_fio=op.full_name,
            group_id=op.group_id,
            group_name=op.group.name if op.group else None,
            day=log.day,
            shift_code=log.shift_code_snapshot,
            start_at=log.start_at,
            end_at=log.end_at,
            aftercall_duration=log.aftercall_duration,
            busy_duration=log.busy_duration,
            hold_duration=log.hold_duration,
            idle_duration=log.idle_duration,
            lazy_duration=log.lazy_duration,
            lock_duration=log.lock_duration,
            relax_duration=log.relax_duration,
            full_duration=log.full_duration,
            is_special_aggregation=log.is_special_aggregation,
        ))

    if rows:
        ArchiveWorkLogDaily.objects.using("archive").bulk_create(
            rows, batch_size=500,
        )

    return len(rows)


# =============================================================================
# ManualAdjustment
# =============================================================================

def _archive_manual_adjustments(cycle: Cycle) -> int:
    """Архивирует все ручные правки за цикл."""
    from archive.models import ArchiveManualAdjustment

    # Все правки, сделанные в период цикла
    adjustments = ManualAdjustment.objects.filter(
        adjusted_at__date__gte=cycle.start_date,
        adjusted_at__date__lte=cycle.end_date,
    ).select_related("operator", "adjusted_by")

    rows = []
    for adj in adjustments:
        op = adj.operator
        rows.append(ArchiveManualAdjustment(
            archive_year=cycle.year,
            archive_month=cycle.month,
            target_type=adj.target_type,
            target_id=adj.target_id,
            operator_id=op.id if op else None,
            operator_login=op.login_id if op else None,
            field_name=adj.field_name,
            old_value=adj.old_value,
            new_value=adj.new_value,
            reason_code=adj.reason_code,
            reason_text=adj.reason_text,
            adjusted_by_id=adj.adjusted_by_id,
            adjusted_by_username=adj.adjusted_by.username if adj.adjusted_by else "",
            adjusted_at=adj.adjusted_at,
            approved_by_id=adj.approved_by_id,
            approved_at=adj.approved_at,
            original_id=adj.id,
        ))

    if rows:
        ArchiveManualAdjustment.objects.using("archive").bulk_create(
            rows, batch_size=500,
        )

    return len(rows)


# =============================================================================
# EventLog (только warning+)
# =============================================================================

def _archive_event_logs(cycle: Cycle) -> int:
    """Архивирует только важные события (warning/error/critical) за цикл."""
    from archive.models import ArchiveEventLog

    events = EventLog.objects.filter(
        timestamp__date__gte=cycle.start_date,
        timestamp__date__lte=cycle.end_date,
        level__in=["warning", "error", "critical"],
    ).select_related("operator")

    rows = []
    for ev in events:
        op = ev.operator
        rows.append(ArchiveEventLog(
            archive_year=cycle.year,
            archive_month=cycle.month,
            event_type=ev.event_type,
            level=ev.level,
            operator_id=op.id if op else None,
            operator_login=op.login_id if op else None,
            target_type=ev.target_type,
            target_id=ev.target_id,
            message=ev.message,
            payload=ev.payload,
            original_timestamp=ev.timestamp,
            triggered_by_id=ev.triggered_by_id,
        ))

    if rows:
        ArchiveEventLog.objects.using("archive").bulk_create(
            rows, batch_size=500,
        )

    return len(rows)


# =============================================================================
# Очистка default DB
# =============================================================================

@transaction.atomic
def _cleanup_default_db(cycle: Cycle):
    """
    Удаляет все данные цикла из default DB.
    Вариант A: всё стирается, новый цикл начинается с нуля.
    """
    logger.info("[archive_cycle] Очистка default DB для цикла %s", cycle)

    # Ommaviy o'chirish — har WDD uchun signal-recompute qilmaymiz (WorkDebt baribir
    # shu yerda o'chiriladi). Aks holda minglab recompute sikl-yopishni sekinlashtiradi.
    from hourly_locks.signals import skip_wdd_delete_recompute
    skip_wdd_delete_recompute(True)
    try:
        _cleanup_default_db_inner(cycle)
    finally:
        skip_wdd_delete_recompute(False)


def _prune_wfm(cycle: Cycle, comp_ids: list):
    """
    Сообщить WFM, что записи этого (заархивированного) цикла удалены.

    Без этого они остаются в WFM НАВСЕГДА: ingest на стороне WFM — только upsert
    (сам ничего не удаляет), а обычный reconcile в push_cycle_to_wfm скоупится на
    АКТИВНЫЙ цикл, поэтому закрытый цикл в скоуп больше не попадает.

    Передаём то, что РЕАЛЬНО осталось в Django по этому циклу (обычно пусто, но
    transfer в pending/in_progress мог остаться) — WFM удалит всё остальное.
    """
    from hourly_locks.sync.runners_sync import _reconcile

    _reconcile("work-debts", WorkDebt.objects.filter(cycle=cycle), cycle_id=cycle.id)
    _reconcile("work-debt-details", WorkDebtDetail.objects.filter(cycle=cycle), cycle_id=cycle.id)
    _reconcile("compensations", Compensation.objects.filter(cycle=cycle), cycle_id=cycle.id)
    _reconcile("transfers", Transfer.objects.filter(cycle=cycle), cycle_id=cycle.id)
    if comp_ids:
        _reconcile(
            "compensation-debt-links",
            CompensationDebtLink.objects.filter(compensation__cycle=cycle),
            compensation_ids=comp_ids,
        )
    logger.info("[archive_cycle] WFM prune yuborildi (cycle=%s)", cycle.id)


def _cleanup_default_db_inner(cycle: Cycle):
    # Ссылки на компенсации нужны ПОСЛЕ удаления — для скоупа WFM-reconcile по
    # compensation-debt-links (у них нет cycle_id).
    comp_ids = list(Compensation.objects.filter(cycle=cycle).values_list("id", flat=True))

    # 1. CompensationDebtLink (через каскад от Compensation, но на всякий случай)
    CompensationDebtLink.objects.filter(compensation__cycle=cycle).delete()

    # 2. Compensation
    deleted_comp, _ = Compensation.objects.filter(cycle=cycle).delete()

    # 3. Transfer — кроме pending/in_progress (они перенесены на новый цикл
    #    и НЕ должны удаляться). Защита на случай оставшихся.
    deleted_tr, _ = Transfer.objects.filter(cycle=cycle).exclude(
        status__in=["pending", "in_progress"],
    ).delete()

    # 4. WorkDebtDetail
    deleted_wdd, _ = WorkDebtDetail.objects.filter(cycle=cycle).delete()

    # 5. WorkDebt
    deleted_wd, _ = WorkDebt.objects.filter(cycle=cycle).delete()

    # 6. WorkLogDaily
    deleted_logs, _ = WorkLogDaily.objects.filter(cycle=cycle).delete()

    # 7. ManualAdjustment за цикл (опционально — можно оставить в default для аудита)
    # ManualAdjustment.objects.filter(
    #     adjusted_at__date__gte=cycle.start_date,
    #     adjusted_at__date__lte=cycle.end_date,
    # ).delete()

    logger.info(
        "[archive_cycle] Удалено: comp=%d, transfer=%d, wdd=%d, wd=%d, logs=%d",
        deleted_comp, deleted_tr, deleted_wdd, deleted_wd, deleted_logs,
    )

    # Зеркалим удаление в WFM (иначе закрытый цикл останется там навсегда).
    # _reconcile сам глотает ошибки — сбой WFM не ломает закрытие цикла.
    _prune_wfm(cycle, comp_ids)