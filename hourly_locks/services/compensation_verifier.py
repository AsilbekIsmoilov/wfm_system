"""
Proverka zayavok na kompensatsiyu.
IDEMPOTENT: snachala rollback ranee verified, zatem zanovo.
Cross-DB credit, NB-kompensatsiya, Benefits/Transfer hisobga olish.
"""

import logging
from datetime import date, timedelta

from django.db import transaction
from django.utils.timezone import now

from hourly_locks.models import (
    Compensation,
    CompensationDebtLink,
    OperatorScheduleDay,
    WorkDebt,
    WorkDebtDetail,
    WorkLogDaily,
)

from . import external_api
from .cycle import get_or_create_active_cycle
from .event_log import log_event

logger = logging.getLogger(__name__)
DEFAULT_TOLERANCE = timedelta(minutes=5)


def verify_compensations(for_date):
    cycle = get_or_create_active_cycle()
    rolled_back = rollback_compensations_for_date(for_date)
    if rolled_back:
        logger.info("[verify_compensations] Otkacheno %d", rolled_back)

    plans = Compensation.objects.filter(
        planned_date=for_date, status="pending",
    ).select_related("operator", "type_rule")

    if not plans.exists():
        # Rollback bo'lgan bo'lsa ham — recompute zarur
        if rolled_back:
            from .work_debt import recompute_for_date
            recompute_for_date(for_date)
        return {"verified": 0, "rolled_back": rolled_back}

    stats = {"approved": 0, "partial": 0, "declined": 0, "skipped": 0, "rolled_back": rolled_back}
    for plan in plans:
        try:
            result = verify_single_compensation(plan, for_date, cycle)
            stats[result] = stats.get(result, 0) + 1
        except Exception as exc:
            logger.exception("Error: %s", exc)
            stats["errors"] = stats.get("errors", 0) + 1

    # Pipeline oxirida — barcha ta'sirlangan operator'lar uchun recompute
    from .work_debt import recompute_for_date
    stats["recomputed"] = recompute_for_date(for_date)

    log_event(
        event_type="compensation_verified", level="info",
        message="Verification done", cycle=cycle, payload=stats,
    )
    return stats


def verify_single_compensation(compensation, for_date, cycle):
    rule = compensation.type_rule
    strategy = rule.verification_strategy

    if strategy == "auto_approve" or rule.auto_approve_on_create:
        return _apply_auto_approve(compensation)
    if strategy == "manual_only":
        return "skipped"
    if strategy == "night_pipeline":
        return "skipped"
    if strategy == "retroactive_check":
        from .retroactive_check import auto_check_retroactive
        return auto_check_retroactive(compensation, for_date, cycle)

    fact_full, fact_lock = _fetch_fact_for_day(compensation.operator, for_date)

    if strategy == "schedule_based":
        return _verify_schedule_based(compensation, for_date, cycle, fact_full, fact_lock)
    if strategy == "net_based":
        return _verify_net_based(compensation, for_date, cycle, fact_full, fact_lock)
    if strategy == "day_off_based":
        return _verify_day_off_based(compensation, for_date, cycle, fact_full, fact_lock)
    return "skipped"


def _get_exempt_duration_for_day(operator, for_date):
    """
    Operator uchun shu kunda aktiv Transfer-larning umumiy vaqti.
    benefits, sl, wc, study, vacation va h.k. (exempts_from_daily_debt=True)
    Bu vaqt kompensatsiya tekshiruvida norma-dan ayiriladi.
    """
    from hourly_locks.models import RequestTypeRule, Transfer

    exempt_rules = RequestTypeRule.objects.filter(
        category="transfer",
        exempts_from_daily_debt=True,
        is_active=True,
    )
    transfers = Transfer.objects.filter(
        operator=operator,
        type_rule__in=exempt_rules,
        status__in=["pending", "in_progress", "approved", "completed"],
        date_from__lte=for_date,
        date_to__gte=for_date,
    )
    total = timedelta(0)
    for tr in transfers:
        if tr.requested_duration:
            total += tr.requested_duration
    if total > timedelta(0):
        logger.info("[exempt] Operator %s da %s aktiv Transfer vaqt = %s",
                    operator, for_date, total)
    return total


def _fetch_fact_for_day(operator, for_date):
    log = WorkLogDaily.objects.filter(operator=operator, day=for_date).first()
    if log:
        return log.full_duration or timedelta(0), log.lock_duration or timedelta(0)

    schedule = OperatorScheduleDay.objects.filter(
        operator=operator, day=for_date,
    ).select_related("shift").first()

    # 24ч-ПЛИТКА (08:00 → 08:00 след.дня) для ночных смен и выходных дней
    # отработки: дни отработки идут подряд без перекрытия (нет двойного счёта)
    # и захватывается отработка днём ДО начала ночной смены (напр. 20-08).
    shift_code = schedule.shift.code if (schedule and schedule.shift) else None
    is_night_tile = shift_code in {"17-02", "18-03", "15-24", "20-08"}
    is_day_off = (not schedule) or schedule.is_day_off or not schedule.shift_id

    if is_night_tile or is_day_off:
        day_data = external_api.fetch_hours_range(for_date, range(8, 24))
        next_data = external_api.fetch_hours_range(for_date + timedelta(days=1), range(0, 8))
    else:
        day_data = external_api.fetch_hours_range(for_date, range(6, 23))
        next_data = {}

    fact_full = timedelta()
    fact_lock = timedelta()
    for rows in day_data.values():
        delta = external_api.sum_for_login(rows, operator.login_id)
        fact_full += delta["full_duration"]
        fact_lock += delta["lock_duration"]
    for rows in next_data.values():
        delta = external_api.sum_for_login(rows, operator.login_id)
        fact_full += delta["full_duration"]
        fact_lock += delta["lock_duration"]
    return fact_full, fact_lock


def _fetch_best_window_for_nb(operator, for_date, default_full, default_lock):
    """12h nb_compensation uchun kunduz va tungi oynani solishtirib, kattasini olamiz."""
    day_full = default_full
    day_lock = default_lock

    night_full = timedelta()
    night_lock = timedelta()
    next_date = for_date + timedelta(days=1)
    night_part1 = external_api.fetch_hours_range(for_date, range(20, 24))
    night_part2 = external_api.fetch_hours_range(next_date, range(0, 9))

    for rows in night_part1.values():
        delta = external_api.sum_for_login(rows, operator.login_id)
        night_full += delta["full_duration"]
        night_lock += delta["lock_duration"]
    for rows in night_part2.values():
        delta = external_api.sum_for_login(rows, operator.login_id)
        night_full += delta["full_duration"]
        night_lock += delta["lock_duration"]

    day_net = max(day_full - day_lock, timedelta(0))
    night_net = max(night_full - night_lock, timedelta(0))

    if night_net > day_net:
        return night_full, night_lock, "night"
    return day_full, day_lock, "day"


def _verify_schedule_based(compensation, for_date, cycle, fact_full, fact_lock):
    """
    Ish kuni uchun oddiy compensation.

    MANTIQ (workday):
      - Smena normasi Transfer.duration miqdorida KAMAYTIRILADI
        (effective_norm = shift.norm_full - exempt_duration)
      - Compensation credit = (fact - effective_norm) — faqat haqiqiy overtime
      - Demak Transfer training/time_off/etc. norm'ni kamaytiradi,
        Compensation esa kamaytirilgan normadan TASHQARI ishlangan vaqtni oladi.

    Misol: Shift 9h, Transfer.training 2h, Comp 1h, operator 8h ishlasa:
      effective_norm = 9 - 2 = 7h
      overtime = 8 - 7 = 1h
      credit = min(1h, 1h) = 1h  →  approved
    """
    operator = compensation.operator
    schedule = OperatorScheduleDay.objects.filter(
        operator=operator, day=for_date,
    ).select_related("shift").first()
    if not schedule or not schedule.shift:
        return _mark_declined(compensation, "Net raspisaniya")

    shift = schedule.shift

    # Transfer duration ni hisobga olib normani kamaytirish
    exempt_duration = _get_exempt_duration_for_day(operator, for_date)
    effective_norm_full = max(shift.norm_full - exempt_duration, timedelta(0))

    overtime = max(fact_full - effective_norm_full, timedelta(0))
    over_lock = max(fact_lock - shift.norm_lock_soft_cap, timedelta(0))
    effective = max(overtime - over_lock, timedelta(0))
    credit = min(compensation.requested_duration, effective)
    return _apply_credit_result(
        compensation, for_date, cycle, fact_full, fact_lock,
        credit, shift.norm_full, shift.norm_lock_soft_cap, "COMPENSATION",
    )


def _verify_net_based(compensation, for_date, cycle, fact_full, fact_lock):
    """
    NB-kompensatsiya:
      - faqat dam olish kunida (yoki schedule yo'q)
      - 9h yoki 12h
      - 12h uchun kunduz/tungi oyna kattasi
      - Benefits/Transfer ham hisobga olinadi
    """
    operator = compensation.operator
    requested = compensation.requested_duration

    is_12h = requested >= timedelta(hours=12)
    minimum_net = timedelta(hours=9, minutes=40) if is_12h else timedelta(hours=7, minutes=30)
    norm_full = timedelta(hours=12) if is_12h else timedelta(hours=9)
    norm_lock = timedelta(hours=2, minutes=20) if is_12h else timedelta(hours=1, minutes=46)

    schedule = OperatorScheduleDay.objects.filter(
        operator=operator, day=for_date,
    ).select_related("shift").first()

    is_day_off = (
        schedule is None or schedule.is_day_off or schedule.shift is None
    )
    if not is_day_off:
        return _mark_declined(
            compensation,
            "NB-kompensatsiya faqat dam olish kunida bajariladi. "
            "Bu kun ({}) uchun smena: {}.".format(
                for_date, schedule.shift.code if schedule and schedule.shift else "?"
            ),
        )

    if is_12h:
        fact_full, fact_lock, work_window = _fetch_best_window_for_nb(
            operator, for_date, fact_full, fact_lock,
        )
    else:
        work_window = "day"

    # DAM KUNI MANTIQI (Variant A): Transfer birinchi to'la bajariladi,
    # qoldiq compensation'ga ketadi.
    transfer_duration = _get_exempt_duration_for_day(operator, for_date)
    net = max(fact_full - fact_lock, timedelta(0))
    # Transfer "o'z ulushini" oladi (net'dan ayriladi)
    comp_net = max(net - transfer_duration, timedelta(0))
    # Endi NB minimum'ni comp_net bilan taqqoslaymiz
    remaining = max(minimum_net - comp_net, timedelta(0))

    if remaining == timedelta(0):
        credit = requested
        status = "approved"
    elif comp_net > timedelta(0):
        credit = comp_net
        status = "partial"
    else:
        credit = timedelta(0)
        status = "declined"

    remaining_debt = max(requested - credit, timedelta(0))
    note = _build_note(fact_full, fact_lock, credit, requested, status, remaining_debt)
    note = "[window={} transfer={}] ".format(work_window, transfer_duration) + note
    _finalize_compensation(
        compensation, for_date, cycle, status, credit, remaining_debt,
        norm_full, norm_lock, fact_full, fact_lock, note, "NB_COMPENSATION",
    )
    return status


def _verify_day_off_based(compensation, for_date, cycle, fact_full, fact_lock):
    """
    Day-off kompensatsiyasi.
    Variant A: Transfer birinchi to'la, qoldiq comp'ga.
    """
    operator = compensation.operator
    transfer_duration = _get_exempt_duration_for_day(operator, for_date)

    net = max(fact_full - fact_lock, timedelta(0))
    comp_net = max(net - transfer_duration, timedelta(0))  # Transfer ulushini ayiramiz
    credit = min(compensation.requested_duration, comp_net)
    remaining_debt = max(compensation.requested_duration - credit, timedelta(0))

    if remaining_debt <= timedelta(0):
        status = "approved"
    elif credit > timedelta(0):
        status = "partial"
    else:
        status = "declined"

    note = _build_note(fact_full, fact_lock, credit, compensation.requested_duration, status, remaining_debt)
    _finalize_compensation(
        compensation, for_date, cycle, status, credit, remaining_debt,
        compensation.requested_duration, timedelta(0),
        fact_full, fact_lock, note, "DAY_OFF_COMPENSATION",
    )
    return status


def _apply_credit_result(compensation, for_date, cycle, fact_full, fact_lock, credit, norm_full, norm_lock, schedule_source):
    plan = compensation.requested_duration
    tolerance = DEFAULT_TOLERANCE

    if credit + tolerance >= plan:
        credit = plan
        status = "approved"
    elif credit > timedelta(0):
        status = "partial"
    else:
        status = "declined"

    remaining_debt = max(plan - credit, timedelta(0))
    note = _build_note(fact_full, fact_lock, credit, plan, status, remaining_debt)
    _finalize_compensation(
        compensation, for_date, cycle, status, credit, remaining_debt,
        norm_full, norm_lock, fact_full, fact_lock, note, schedule_source,
    )
    return status


def _finalize_compensation(
    compensation, for_date, cycle, status, credit, remaining_debt,
    norm_full, norm_lock, fact_full, fact_lock, note, schedule_source,
):
    with transaction.atomic():
        compensation.verified_duration = credit
        compensation.remaining_debt = remaining_debt
        compensation.status = status
        compensation.verified_at = now()
        compensation.save(update_fields=[
            "verified_duration", "remaining_debt", "status",
            "verified_at", "updated_at",
        ])

        if status in ("approved", "partial") and credit > timedelta(0):
            _apply_debt_links(compensation, credit, cycle)

        if status == "partial" and remaining_debt > timedelta(0):
            source_key = (
                "nb_compensation_shortfall"
                if "NB" in schedule_source
                else "compensation_shortfall"
            )
            # Shortfall WDD — informatsion yozuv: locked WDD ning to'lanmagan qismi.
            # WorkDebt'ga QO'SHILMAYDI — chunki:
            #   1. Original WDD allaqachon current_debt va total_accumulated ga
            #      qo'shilgan edi (debt_calculator orqali)
            #   2. _apply_debt_links credit qismni current_debt dan ayirgan
            #   3. Qoldiq (shortfall) AVTOMATIK current_debt da turibdi
            #   Shortfall WDD ni qo'shsak — 2x sanash bo'ladi
            WorkDebtDetail.objects.update_or_create(
                operator=compensation.operator,
                day=for_date,
                source=source_key,
                source_object_id=compensation.id,
                defaults={
                    "cycle": cycle,
                    "violation_type": "insufficient_wh",
                    "norm_full": norm_full,
                    "fact_full": credit,
                    "debt_full": remaining_debt,
                    "norm_lock": norm_lock,
                    "fact_lock": fact_lock,
                    "debt_lock": timedelta(0),
                    "make_up_wh": credit,
                    "note": note,
                    "locked_for_compensation": True,  # bu ham locked — qarz emas
                },
            )


def _apply_debt_links(compensation, credit, cycle):
    """
    Compensation kreditni bog'langan qarz yozuvlariga qo'llaydi.

    QOIDA: WorkDebt'ga to'g'ridan-to'g'ri tegmaymiz —
    `work_debt.recompute_for_operator` orqali yangilanadi.

    Faqat CompensationDebtLink va WorkDebtDetail ga tegamiz.
    """
    operator = compensation.operator
    remaining_credit = credit

    links = (
        CompensationDebtLink.objects
        .filter(compensation=compensation, applied=False)
        .select_related("debt_detail")
    )

    for link in links:
        if remaining_credit <= timedelta(0):
            break

        if link.debt_detail:
            wdd = link.debt_detail
            wdd_total = wdd.debt_full + wdd.debt_lock
            applied = min(wdd_total, remaining_credit)

            link.applied = True
            link.applied_amount = applied
            link.save(update_fields=["applied", "applied_amount", "updated_at"])

            wdd.locked_for_compensation = True
            note_extra = "\nZakryto zayavkoy #{} ({}): {}".format(
                compensation.id, compensation.planned_date, applied,
            )
            wdd.note = (wdd.note or "") + note_extra
            wdd.save(update_fields=["locked_for_compensation", "note", "updated_at"])

            remaining_credit -= applied
            continue

        snap = link.snapshot or {}
        if "archive_year" in snap and "archive_month" in snap:
            applied = _apply_to_archive(link, snap, operator, remaining_credit, compensation)
            if applied > timedelta(0):
                remaining_credit -= applied

    compensation.deducted = True
    compensation.save(update_fields=["deducted", "updated_at"])

    # WorkDebt'ni qayta hisoblash (single source of truth)
    from .work_debt import recompute_for_operator
    recompute_for_operator(operator, cycle)


def _apply_to_archive(link, snapshot, operator, available_credit, compensation):
    from archive.models import ArchiveWorkDebt, ArchiveWorkDebtDetail

    archive_year = snapshot["archive_year"]
    archive_month = snapshot["archive_month"]
    day = snapshot.get("day")
    source = snapshot.get("source")

    archived_wdd = (
        ArchiveWorkDebtDetail.objects.using("archive").filter(
            archive_year=archive_year, archive_month=archive_month,
            operator_id=operator.id, day=day, source=source,
        ).first()
    )
    if not archived_wdd:
        return timedelta(0)

    archived_total = archived_wdd.debt_full + archived_wdd.debt_lock
    applied = min(archived_total, available_credit)

    archived_wdd.locked_for_compensation = True
    archived_wdd.note = (archived_wdd.note or "") + (
        "\n[Post-cycle credit] Comp#{} ({}): {}".format(
            compensation.id, compensation.planned_date, applied,
        )
    )
    archived_wdd.save(using="archive", update_fields=["locked_for_compensation", "note"])

    archived_wd = (
        ArchiveWorkDebt.objects.using("archive").filter(
            archive_year=archive_year, archive_month=archive_month,
            operator_id=operator.id,
        ).first()
    )
    if archived_wd:
        archived_wd.final_debt = max(archived_wd.final_debt - applied, timedelta(0))
        archived_wd.save(using="archive", update_fields=["final_debt"])

    link.applied = True
    link.applied_amount = applied
    link.save(update_fields=["applied", "applied_amount", "updated_at"])

    log_event(
        event_type="compensation_verified", level="info",
        operator=operator, target_type="compensation", target_id=compensation.id,
        message="Cross-DB credit primenen",
        payload={"archive_year": archive_year, "archive_month": archive_month, "applied": str(applied)},
    )
    return applied


def _mark_declined(compensation, reason):
    compensation.status = "declined"
    compensation.comment = (compensation.comment or "") + "\nOtklonenо: " + reason
    compensation.verified_at = now()
    compensation.save(update_fields=["status", "comment", "verified_at", "updated_at"])
    return "declined"


def _apply_auto_approve(compensation):
    """
    Avto-tasdiq (exception, no_compensation, sl, wc, study, vacation).
    Agar related_debts_input yuborilmagan bo'lsa, operatorning ochiq
    qarzlarini avto-bog'laydi (FIFO — eskidan boshlab).
    """
    compensation.status = "approved"
    compensation.verified_duration = compensation.requested_duration
    compensation.verified_at = now()
    compensation.save(update_fields=[
        "status", "verified_duration", "verified_at", "updated_at",
    ])
    cycle = compensation.cycle
    with transaction.atomic():
        # Agar hech qanday CompensationDebtLink yo'q bo'lsa,
        # operatorning ochiq qarzlarini avto-bog'laymiz (FIFO).
        _ensure_debt_links_or_auto_attach(compensation, cycle)
        _apply_debt_links(compensation, compensation.requested_duration, cycle)
    return "approved"


def _ensure_debt_links_or_auto_attach(compensation, cycle):
    """
    Agar CompensationDebtLink yo'q bo'lsa, operatorning ochiq qarzlarini
    eskidan boshlab tanlab, requested_duration cheklovida bog'laydi.
    """
    if CompensationDebtLink.objects.filter(compensation=compensation).exists():
        return  # allaqachon bor

    credit = compensation.requested_duration or timedelta(0)
    if credit <= timedelta(0):
        return

    open_wdds = WorkDebtDetail.objects.filter(
        operator=compensation.operator,
        cycle=cycle,
        locked_for_compensation=False,
    ).exclude(
        debt_full=timedelta(0), debt_lock=timedelta(0),
    ).order_by("day", "id")

    remaining = credit
    created = 0
    for wdd in open_wdds:
        if remaining <= timedelta(0):
            break
        wdd_total = (wdd.debt_full or timedelta(0)) + (wdd.debt_lock or timedelta(0))
        if wdd_total <= timedelta(0):
            continue

        snap = {
            "id": wdd.id,
            "day": str(wdd.day),
            "source": wdd.source,
            "shift_code": wdd.shift_code_snapshot,
            "norm_full": str(wdd.norm_full),
            "fact_full": str(wdd.fact_full),
            "debt_full": str(wdd.debt_full),
            "norm_lock": str(wdd.norm_lock),
            "fact_lock": str(wdd.fact_lock),
            "debt_lock": str(wdd.debt_lock),
            "note": wdd.note or "",
            "auto_attached": True,
        }
        CompensationDebtLink.objects.create(
            compensation=compensation,
            debt_detail=wdd,
            snapshot=snap,
            applied=False,
        )
        remaining -= min(wdd_total, remaining)
        created += 1

    if created:
        logger.info(
            "[auto_attach] Comp #%d ga %d ta ochiq qarz avto-bog'landi",
            compensation.id, created,
        )


def _build_note(fact_full, fact_lock, credit, plan, status, remaining_debt):
    return "Otrabotano: {} ({})\nPlan: {}\nZachteno: {}\nOstalos: {}\nStatus: {}".format(
        fact_full, fact_lock, plan, credit, remaining_debt, status,
    )


# =============================================================================
# ROLLBACK
# =============================================================================

def rollback_compensations_for_date(for_date):
    verified = Compensation.objects.filter(
        planned_date=for_date,
    ).exclude(status="pending").select_related("operator", "cycle")

    count = 0
    for comp in verified:
        try:
            _rollback_single_compensation(comp)
            count += 1
        except Exception as exc:
            logger.exception("[rollback] Oshibka dlya Comp#%d: %s", comp.id, exc)
    return count


@transaction.atomic
def _rollback_single_compensation(comp):
    """
    Compensation rollback.
    QOIDA: WorkDebt'ga tegmaymiz — oxirida recompute chaqiriladi.
    """
    operator = comp.operator
    cycle = comp.cycle

    applied_links = CompensationDebtLink.objects.filter(
        compensation=comp, applied=True,
    ).select_related("debt_detail")

    for link in applied_links:
        applied_amount = link.applied_amount or timedelta(0)

        if link.debt_detail:
            wdd = link.debt_detail
            wdd.locked_for_compensation = False
            if wdd.note and "#{}".format(comp.id) in wdd.note:
                lines = wdd.note.split("\n")
                lines = [l for l in lines if "#{}".format(comp.id) not in l]
                wdd.note = "\n".join(lines)
            wdd.save(update_fields=["locked_for_compensation", "note", "updated_at"])

        snap = link.snapshot or {}
        if "archive_year" in snap and not link.debt_detail:
            _rollback_archive_credit(snap, operator, applied_amount, comp.id)

        link.applied = False
        link.applied_amount = None
        link.save(update_fields=["applied", "applied_amount", "updated_at"])

    # Shortfall WDD'ni o'chiramiz — WorkDebt'ga tegmaymiz
    WorkDebtDetail.objects.filter(
        operator=operator,
        source__in=["compensation_shortfall", "nb_compensation_shortfall"],
        source_object_id=comp.id,
    ).delete()

    comp.status = "pending"
    comp.verified_duration = None
    comp.remaining_debt = None
    comp.verified_at = None
    comp.deducted = False
    comp.save(update_fields=[
        "status", "verified_duration", "remaining_debt",
        "verified_at", "deducted", "updated_at",
    ])

    # WorkDebt'ni qayta hisoblash
    from .work_debt import recompute_for_operator
    recompute_for_operator(operator, cycle)


def _rollback_archive_credit(snapshot, operator, applied_amount, comp_id):
    from archive.models import ArchiveWorkDebt, ArchiveWorkDebtDetail

    archive_year = snapshot["archive_year"]
    archive_month = snapshot["archive_month"]
    day = snapshot.get("day")
    source = snapshot.get("source")

    awdd = (
        ArchiveWorkDebtDetail.objects.using("archive").filter(
            archive_year=archive_year, archive_month=archive_month,
            operator_id=operator.id, day=day, source=source,
        ).first()
    )
    if awdd:
        awdd.locked_for_compensation = False
        awdd.save(update_fields=["locked_for_compensation"])

    awd = (
        ArchiveWorkDebt.objects.using("archive").filter(
            archive_year=archive_year, archive_month=archive_month,
            operator_id=operator.id,
        ).first()
    )
    if awd:
        awd.current_debt = awd.current_debt + applied_amount
        awd.save(update_fields=["current_debt"])
