
import logging
from datetime import date, timedelta
from typing import Optional

from django.db import transaction
from django.utils.dateparse import parse_duration
from django.utils.timezone import now

from hourly_locks.models import (
    OperatorScheduleDay,
    RequestTypeRule,
    Transfer,
    WorkDebt,
    WorkDebtDetail,
    WorkLogDaily,
)

from . import external_api
from . import shift as shift_service
from .cycle import get_or_create_active_cycle
from .event_log import log_event

logger = logging.getLogger(__name__)

DEFAULT_TOLERANCE = timedelta(minutes=5)


def update_transfer_statuses(today: Optional[date] = None) -> dict:
    if today is None:
        today = date.today()

    started = Transfer.objects.filter(
        status="pending",
        date_from__lte=today,
        date_to__gte=today,
    )
    count_started = started.update(status="in_progress")
    if count_started:
        logger.info("[transfer_lifecycle] %s zayavok in_progress", count_started)

    auto_complete_rules = RequestTypeRule.objects.filter(
        category="transfer",
        verification_strategy__in=["auto_approve", "date_range_based"],
    )
    ended = Transfer.objects.filter(
        type_rule__in=auto_complete_rules,
        status__in=["pending", "in_progress"],
        date_to__lt=today,
    )
    count_ended = ended.update(status="completed")
    if count_ended:
        logger.info("[transfer_lifecycle] %s zayavok completed", count_ended)

    return {"started": count_started, "ended": count_ended}


def verify_transfers_for_date(for_date):
    cycle = get_or_create_active_cycle()
    rolled_back = rollback_transfers_for_date(for_date)
    if rolled_back:
        logger.info("[verify_transfers] Otkacheno %d transfers", rolled_back)

    transfers = Transfer.objects.filter(
        date_to=for_date,
        type_rule__code="transfer",
        status__in=["pending", "in_progress"],
    ).select_related("operator", "type_rule")

    if not transfers.exists():
        return {"verified": 0, "rolled_back": rolled_back}

    stats = {"completed": 0, "partial": 0, "declined": 0, "skipped": 0, "rolled_back": rolled_back}
    for transfer in transfers:
        try:
            result = _verify_single_transfer(transfer, cycle)
            stats[result] = stats.get(result, 0) + 1
        except Exception as exc:
            logger.exception("[transfer_verifier] Oshibka dlya %s: %s", transfer, exc)
            stats["errors"] = stats.get("errors", 0) + 1

    log_event(
        event_type="transfer_completed", level="info",
        message="Proverka perenosov", cycle=cycle, payload=stats,
    )

    from .work_debt import recompute_for_date
    stats["recomputed"] = recompute_for_date(for_date)
    return stats


def _fetch_day_facts(operator, day):
    log = WorkLogDaily.objects.filter(operator=operator, day=day).first()
    if log:
        return (
            log.full_duration or timedelta(),
            log.lock_duration or timedelta(),
        )

    day_data = external_api.fetch_hours_range(day, range(6, 24))
    fact_full = timedelta()
    fact_lock = timedelta()
    for rows in day_data.values():
        delta = external_api.sum_for_login(rows, operator.login_id)
        fact_full += delta.get("full_duration", timedelta())
        fact_lock += delta.get("lock_duration", timedelta())

    return fact_full, fact_lock


def _verify_single_transfer(transfer, cycle):
    operator = transfer.operator

    src_schedule = OperatorScheduleDay.objects.filter(
        operator=operator, day=transfer.date_from,
    ).select_related("shift").first()

    if not src_schedule or not src_schedule.shift:
        # comment — faqat user qiymati; tizim unga yozmaydi.
        transfer.status = "declined"
        transfer.verified_at = now()
        transfer.save(update_fields=["status", "verified_at", "updated_at"])
        return "declined"

    shift_obj = src_schedule.shift
    norm_full = shift_obj.norm_full
    norm_lock = shift_obj.norm_lock_soft_cap
    tolerance = DEFAULT_TOLERANCE

    fact_full_from, fact_lock_from = _fetch_day_facts(operator, transfer.date_from)
    fact_full_to, fact_lock_to = _fetch_day_facts(operator, transfer.date_to)

    fact_full = fact_full_from + fact_full_to
    fact_lock = fact_lock_from + fact_lock_to

    if fact_full <= timedelta(0):
        # comment — faqat user qiymati; tizim unga yozmaydi.
        transfer.status = "declined"
        transfer.verified_at = now()
        transfer.save(update_fields=["status", "verified_at", "updated_at"])
        return "declined"

    # === Full duration tekshiruvi ===
    if fact_full + tolerance >= norm_full:
        full_debt = timedelta(0)
    else:
        full_debt = norm_full - fact_full

    # === Lock (tanaffus) tekshiruvi (debt_calculator.check_debt_for_log dan ko'chirildi) ===
    lock_debt = timedelta(0)
    warn_at = shift_obj.norm_lock_warn_at or timedelta(0)
    extra_full_pos = max(fact_full - norm_full, timedelta(0))  # pererabotka tanaffusni qoplaydi
    is_9h_logic = warn_at > timedelta(0)

    if is_9h_logic:
        # 9 soatlik smena: warn_at dan boshlab jarima
        if fact_lock > warn_at:
            effective_overbreak = (fact_lock - warn_at) - extra_full_pos
            if effective_overbreak > timedelta(0):
                lock_debt = effective_overbreak
    else:
        # 12 soatlik smena yoki tolerance=0 holat: norm_lock dan boshlab
        if fact_lock > norm_lock:
            extra_break = fact_lock - norm_lock
            effective_overbreak = extra_break - extra_full_pos
            if effective_overbreak > timedelta(0):
                lock_debt = effective_overbreak

    # === Yakuniy status ===
    debt = full_debt + lock_debt
    if debt <= timedelta(0):
        status = "completed"
    elif fact_full > timedelta(0):
        status = "partial"
    else:
        status = "declined"

    with transaction.atomic():
        transfer.verified_duration = fact_full
        transfer.remaining_debt = debt
        transfer.status = status
        transfer.verified_at = now()
        transfer.save(update_fields=[
            "verified_duration", "remaining_debt", "status",
            "verified_at", "updated_at",
        ])

        if status in ["partial", "declined"] and debt > timedelta(0):
            # Buzilish turini aniqlash
            if full_debt > timedelta(0) and lock_debt > timedelta(0):
                violation = "both_violations"
            elif full_debt > timedelta(0):
                violation = "insufficient_wh"
            else:
                violation = "exceeding_break"

            note = (
                "Perenos smeny ({status})\n"
                "Smena {from_d} ({code}): norm_full {norm}, norm_lock {nl} (warn {wa})\n"
                "Fakt {from_d}: full={ff_from}, lock={fl_from}\n"
                "Fakt {to_d}: full={ff_to}, lock={fl_to}\n"
                "Jami: full={ff_total}, lock={fl_total}\n"
                "Qarz: full={fd}, lock={ld}, jami={debt}"
            ).format(
                status=status, from_d=transfer.date_from, to_d=transfer.date_to,
                code=shift_obj.code, norm=norm_full, nl=norm_lock, wa=warn_at,
                ff_from=fact_full_from, fl_from=fact_lock_from,
                ff_to=fact_full_to, fl_to=fact_lock_to,
                ff_total=fact_full, fl_total=fact_lock,
                fd=full_debt, ld=lock_debt, debt=debt,
            )
            WorkDebtDetail.objects.update_or_create(
                operator=operator,
                day=transfer.date_to,
                source="transfer",
                source_object_id=transfer.id,
                defaults={
                    "cycle": cycle,
                    "shift": shift_obj,
                    "shift_code_snapshot": shift_obj.code,
                    "violation_type": violation,
                    "norm_full": norm_full,
                    "fact_full": fact_full,
                    "debt_full": full_debt,
                    "norm_lock": norm_lock,
                    "fact_lock": fact_lock,
                    "debt_lock": lock_debt,
                    "make_up_wh": extra_full_pos,
                    "note": note,
                },
            )

    # WorkDebt qayta hisoblash
    from .work_debt import recompute_for_operator
    recompute_for_operator(operator, cycle)

    return status


def apply_time_off_extended_window(for_date):
    """
    Obrabatyvaet zayavki time_off.
    IDEMPOTENT: snachala rollback, zatem zanovo.
    """
    cycle = get_or_create_active_cycle()
    rolled_back = rollback_time_offs_for_date(for_date)
    if rolled_back:
        logger.info("[apply_time_off] Otkacheno %d time_off", rolled_back)

    time_offs = Transfer.objects.filter(
        type_rule__code="time_off",
        status__in=["pending", "in_progress"],
        date_from__lte=for_date,
        date_to__gte=for_date,
    ).select_related("operator", "type_rule")

    if not time_offs.exists():
        return {"processed": 0, "rolled_back": rolled_back}

    stats = {"approved": 0, "partial": 0, "declined": 0, "rolled_back": rolled_back}
    for off in time_offs:
        try:
            result = _verify_single_time_off(off, for_date, cycle)
            stats[result] = stats.get(result, 0) + 1
        except Exception as exc:
            logger.exception("[time_off] Oshibka dlya %s: %s", off, exc)
            stats["errors"] = stats.get("errors", 0) + 1

    return stats


def _verify_single_time_off(off, for_date, cycle):
    if not off.requested_duration or off.requested_duration <= timedelta(0):
        return "skipped"

    operator = off.operator
    schedule = OperatorScheduleDay.objects.filter(
        operator=operator, day=for_date,
    ).select_related("shift").first()

    if not schedule or not schedule.shift:
        return "skipped"

    shift_obj = schedule.shift
    norm_full = shift_obj.norm_full
    effective_norm = max(norm_full - off.requested_duration, timedelta(0))

    start_h, end_h, crosses = shift_service.get_fetch_window(shift_obj)
    extra_hours = int(off.requested_duration.total_seconds() // 3600)
    end_h_ext = end_h + extra_hours

    if end_h_ext < 24:
        hours_map = {for_date: range(start_h, end_h_ext + 1)}
    else:
        hours_map = {
            for_date: range(start_h, 24),
            for_date + timedelta(days=1): range(0, (end_h_ext % 24) + 1),
        }

    total_full = timedelta()
    total_lock = timedelta()
    for day, hours in hours_map.items():
        day_data = external_api.fetch_hours_range(day, hours)
        for rows in day_data.values():
            delta = external_api.sum_for_login(rows, operator.login_id)
            total_full += delta["full_duration"]
            total_lock += delta["lock_duration"]

    full_debt = max(effective_norm - total_full, timedelta(0))
    warn_at = shift_obj.norm_lock_warn_at or timedelta(hours=1, minutes=30)
    if total_lock <= warn_at:
        lock_debt = timedelta(0)
    else:
        lock_over = total_lock - warn_at
        extra_full = max(total_full - effective_norm, timedelta(0))
        lock_debt = max(lock_over - extra_full, timedelta(0))

    total_debt = full_debt + lock_debt

    if total_full == timedelta(0):
        status = "declined"
    elif total_debt > timedelta(0):
        status = "partial"
    else:
        status = "approved"

    with transaction.atomic():
        off.status = status
        off.verified_duration = total_full
        off.remaining_debt = total_debt
        off.verified_at = now()
        off.save(update_fields=[
            "status", "verified_duration", "remaining_debt",
            "verified_at", "updated_at",
        ])

    return status


def rollback_transfers_for_date(for_date):
    verified = Transfer.objects.filter(
        date_to=for_date,
        type_rule__code="transfer",
    ).exclude(status__in=["pending", "in_progress"]).select_related("operator", "cycle")

    count = 0
    for tr in verified:
        try:
            _rollback_single_transfer(tr)
            count += 1
        except Exception as exc:
            logger.exception("[rollback_transfer] Oshibka dlya Tr#%d: %s", tr.id, exc)
    return count


@transaction.atomic
def _rollback_single_transfer(transfer):
    operator = transfer.operator
    cycle = transfer.cycle

    WorkDebtDetail.objects.filter(
        operator=operator,
        source="transfer",
        source_object_id=transfer.id,
    ).delete()

    transfer.status = "pending"
    transfer.verified_duration = None
    transfer.remaining_debt = None
    transfer.verified_at = None
    transfer.save(update_fields=[
        "status", "verified_duration", "remaining_debt",
        "verified_at", "updated_at",
    ])

    from .work_debt import recompute_for_operator
    recompute_for_operator(operator, cycle)


def rollback_time_offs_for_date(for_date):
    verified = Transfer.objects.filter(
        type_rule__code="time_off",
        date_from__lte=for_date,
        date_to__gte=for_date,
    ).exclude(status__in=["pending", "in_progress"]).select_related("operator", "cycle")

    count = 0
    for off in verified:
        try:
            _rollback_single_time_off(off, for_date)
            count += 1
        except Exception as exc:
            logger.exception("[rollback_time_off] Oshibka dlya Off#%d: %s", off.id, exc)
    return count


@transaction.atomic
def _rollback_single_time_off(off, for_date):
    operator = off.operator
    cycle = off.cycle

    WorkDebtDetail.objects.filter(
        operator=operator,
        day=for_date,
        source="time_off",
        source_object_id=off.id,
    ).delete()

    off.status = "pending"
    off.verified_duration = None
    off.remaining_debt = None
    off.verified_at = None
    off.save(update_fields=[
        "status", "verified_duration", "remaining_debt",
        "verified_at", "updated_at",
    ])

    from .work_debt import recompute_for_operator
    recompute_for_operator(operator, cycle)