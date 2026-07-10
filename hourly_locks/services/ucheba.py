"""
Учёба в рабочее время (Transfer, тип «ucheba_rabochee»).

Оператор уходит учиться в середине рабочего дня (окно, напр. 12:00–18:00) и
ВОЗВРАЩАЕТСЯ доработать. Норма дня НЕ уменьшается — оператор обязан покрыть
ПОЛНУЮ дневную норму, работая до/после учёбы (в т.ч. поздно, в след. сутки).

Приём (заявка в Transfer, status=pending):
  - оператор(ы), дни (только рабочие), окно времени, comment, файл;
  - 1 заявка / 1 день (запрет дубля); конфликт: otprashivanie, perenos_dnya,
    другая ucheba.

Проверка (по факту, в дневном пайплайне, когда день прошёл):
  - факт берётся из WorkLogDaily (log_loader пишет его по РАСШИРЕННОМУ окну
    для дней учёбы: от начала смены до остановки работы — часы с full=0
    (окно учёбы и хвост) в сумму не входят);
  - долг = max(0, work_norm − отработано) по обычной дневной норме;
  - WorkDebtDetail source="ucheba" если норма не покрыта;
  - отработка идёт ПАРАЛЛЕЛЬНО (доработка сверх нормы — отдельно, без двойного
    счёта: учёба закрывает норму, отработка — сверх нормы);
  - после прохождения end_date и проверки ВСЕХ дней — Transfer → approved.
"""

from datetime import date, timedelta

from django.db import transaction
from django.db.models import Max
from django.utils.timezone import now, localdate

from ..models import (
    OperatorScheduleDay, Operator, RequestTypeRule, Transfer,
    UchebaDay, WorkDebtDetail, WorkLogDaily,
)
from . import external_api, notify
from .cycle import get_or_create_active_cycle, get_cycle_for_date
from .otprashivanie import _window_duration, _parse_time, _is_working_day, _normalize_days
from .work_debt import recompute_for_operator

UCHEBA_CODE = "ucheba_rabochee"
ACTIVE_STATUSES = ["pending", "in_progress", "approved", "partial", "completed"]
CONFLICT_TRANSFER_CODES = ["otprashivanie", "perenos_dnya"]  # + дубль ucheba


class UchebaError(Exception):
    def __init__(self, message, code="error", payload=None, http_status=400):
        super().__init__(message)
        self.message = message; self.code = code
        self.payload = payload or {}; self.http_status = http_status


# --------------------------------------------------------------------------- #
# Нормы / факт
# --------------------------------------------------------------------------- #

def _benefit_exemption(operator, day):
    """
    Параллельные льготы на день учёбы (по матрице с учёбой параллельны
    Исключение/Обучение/Хоз; lgota — конфликт, не попадёт). Уменьшают дневную
    норму так же, как debt_calculator: возвращает (full_day_exempt, partial_dur).
    """
    Z = timedelta(0)
    from ..models import BenefitDay
    full, partial = False, Z
    for bd in BenefitDay.objects.filter(
            day=day, transfer__operator=operator,
            transfer__type_rule__exempts_from_daily_debt=True,
            transfer__status__in=ACTIVE_STATUSES):
        if bd.duration and bd.duration > Z:
            partial += bd.duration
        else:
            full = True
    return full, partial


def _norms(shift):
    Z = timedelta(0)
    if shift.norm_lock_warn_at and shift.norm_lock_warn_at > Z:   # 9ч/8ч
        lock_norm = shift.norm_lock_warn_at
        soft = shift.norm_lock_soft_cap or lock_norm
    else:                                                          # 12ч
        lock_norm = shift.norm_lock_soft_cap or Z
        soft = lock_norm
    work_norm = max((shift.norm_full or Z) - lock_norm, Z)
    grace = max(soft - lock_norm, Z)
    return work_norm, lock_norm, soft, grace


def fetch_extended_fact(operator, day, shift):
    """
    Факт за день учёбы по РАСШИРЕННОМУ окну: от начала смены до 08:00 след.дня.
    Часы с full=0 (окно учёбы и хвост) в сумму не входят автоматически —
    «до остановки работы (full=0)».
    """
    Z = timedelta(0)
    start = shift.start_time.hour if shift else 6
    full = Z; lock = Z
    d1 = external_api.fetch_hours_range(day, range(start, 24))
    d2 = external_api.fetch_hours_range(day + timedelta(days=1), range(0, 8))
    for data in (d1, d2):
        for rows in data.values():
            delta = external_api.sum_for_login(rows, operator.login_id)
            full += delta["full_duration"]
            lock += delta["lock_duration"]
    return full, lock


# --------------------------------------------------------------------------- #
# Конфликты / валидация приёма
# --------------------------------------------------------------------------- #

def get_ucheba_conflicts(operator, days):
    """Конфликты на дни учёбы — ЕДИНАЯ матрица (otprashivanie/perenos/Льготы) +
    запрет дубля учёбы (1 заявка / 1 день)."""
    from .conflicts import find_conflicts
    conflicts = find_conflicts(operator, "ucheba_rabochee", days)
    dup = UchebaDay.objects.filter(
        transfer__operator=operator, transfer__type_rule__code=UCHEBA_CODE,
        transfer__status__in=ACTIVE_STATUSES, day__in=set(days),
    ).values_list("day", flat=True)
    for d in dup:
        conflicts.append({"code": "ucheba_rabochee", "display_name": "Учёба (дубль)",
                          "day": str(d), "id": None})
    return conflicts


def _validate_common(days, hour_from, hour_to):
    cycle = get_or_create_active_cycle()
    try:
        rule = RequestTypeRule.objects.get(code=UCHEBA_CODE, category="transfer", is_active=True)
    except RequestTypeRule.DoesNotExist:
        raise UchebaError("Тип «Учёба в рабочее время» не найден/неактивен.",
                          code="type_missing", http_status=404)
    hour_from = _parse_time(hour_from); hour_to = _parse_time(hour_to)
    if not hour_from or not hour_to:
        raise UchebaError("Не указано окно учёбы (с/по).", code="no_hours")
    window = _window_duration(hour_from, hour_to)
    if window <= timedelta(0):
        raise UchebaError("Окно учёбы должно быть больше нуля.", code="bad_hours")
    days = _normalize_days(days)
    if not days:
        raise UchebaError("Не выбран ни один день.", code="no_days")
    # ЕДИНАЯ проверка дат: в цикле + не в прошлом (ретро для учёбы нет).
    from .cycle import check_request_days
    bad = check_request_days(days, allow_past=False, cycle=cycle)
    if bad:
        b = bad[0]
        if b["code"] == "out_of_cycle":
            raise UchebaError(f"День {b['day']} вне активного цикла ({b['cycle']}).",
                              code="day_out_of_cycle", payload={"violations": bad})
        raise UchebaError(
            f"Заявку на прошедший день ({b['day']}) подать нельзя.",
            code="day_in_past", payload={"violations": bad})
    return {"cycle": cycle, "rule": rule, "days": days,
            "hour_from": hour_from, "hour_to": hour_to, "window": window}


def _operator_plan(operator, ctx):
    non_work = [d for d in ctx["days"] if not _is_working_day(operator, d)]
    conflicts = get_ucheba_conflicts(operator, ctx["days"])
    conf_days = {c["day"] for c in conflicts}
    nonwork_set = set(non_work)
    valid = [d for d in ctx["days"]
             if d not in nonwork_set and str(d) not in conf_days]
    dropped = {"not_working_day": [str(d) for d in non_work], "conflicts": conflicts}
    return valid, dropped


# --------------------------------------------------------------------------- #
# Preview / Accept
# --------------------------------------------------------------------------- #

def preview_ucheba(*, operators, days, hour_from, hour_to) -> dict:
    ctx = _validate_common(days, hour_from, hour_to)
    per_op = []
    for op in operators:
        valid, dropped = _operator_plan(op, ctx)
        per_op.append({"operator_id": op.id, "operator": op.full_name,
                       "ok": bool(valid), "valid_days": [str(d) for d in valid],
                       "dropped": dropped})
    return {"window": str(ctx["window"]), "days": [str(d) for d in ctx["days"]],
            "operators": per_op, "ok": bool(per_op) and all(o["ok"] for o in per_op)}


def apply_ucheba_effects(tr, valid_days, ctx):
    """Установить поля учёбы на tr (status=pending/даты/часы/длительность) и
    пересоздать UchebaDay. Единый источник эффектов учёбы — приём (_accept_one)
    и редактирование (transfer_edit). Редактирование сбрасывает статус в pending
    → повторная верификация в пайплайне. `tr` содержит operator/cycle/type_rule."""
    total = ctx["window"] * len(valid_days)
    tr.status = "pending"
    tr.date_from = min(valid_days)
    tr.date_to = max(valid_days)
    tr.hour_from = ctx["hour_from"]
    tr.hour_to = ctx["hour_to"]
    tr.requested_duration = total
    tr.save()
    tr.ucheba_days.all().delete()
    for d in valid_days:
        UchebaDay.objects.create(
            transfer=tr, day=d, hour_from=ctx["hour_from"], hour_to=ctx["hour_to"],
            duration=ctx["window"], status="pending")
    return tr


def revert_ucheba_effects(tr):
    """Удалить UchebaDay заявки (поля tr перезапишет apply). Идемпотентно."""
    tr.ucheba_days.all().delete()


@transaction.atomic
def _accept_one(operator, valid_days, ctx, comment, user, pdf_file, screens):
    tr = Transfer(
        operator=operator, cycle=ctx["cycle"], type_rule=ctx["rule"],
        comment=comment or "", pdf_file=pdf_file, screens=screens, fixed_by=user,
    )
    apply_ucheba_effects(tr, valid_days, ctx)
    notify.notify_operator(operator, "ucheba_created", {
        "transfer_id": tr.id, "days": [str(d) for d in valid_days],
        "window": str(ctx["window"])})
    return tr


def accept_ucheba(*, operators, days, hour_from, hour_to, comment="",
                  user=None, source="requested", pdf_file=None, screens=None) -> dict:
    ctx = _validate_common(days, hour_from, hour_to)
    if not operators:
        raise UchebaError("Не выбран оператор.", code="no_operators")
    created, skipped = [], []
    for op in operators:
        valid, dropped = _operator_plan(op, ctx)
        if not valid:
            skipped.append({"operator_id": op.id, "operator": op.full_name,
                            "reason": {"code": "no_valid_days", **dropped}})
            continue
        tr = _accept_one(op, valid, ctx, comment, user, pdf_file, screens)
        created.append({"operator_id": op.id, "operator": op.full_name,
                        "transfer_id": tr.id, "days": [str(d) for d in valid],
                        "dropped": dropped})
    if not created and skipped:
        raise UchebaError("Ни одна заявка не принята (выходные/конфликты).",
                          code="all_skipped", payload={"skipped": skipped}, http_status=409)
    return {"created": created, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Проверка по факту
# --------------------------------------------------------------------------- #

@transaction.atomic
def verify_ucheba_day(uday) -> dict:
    """Проверить один день учёбы: полная норма vs факт (расширенное окно)."""
    Z = timedelta(0)
    tr = uday.transfer
    op = tr.operator
    day = uday.day
    cycle = get_cycle_for_date(day) or tr.cycle

    sch = (OperatorScheduleDay.objects.filter(operator=op, day=day)
           .select_related("shift").first())
    shift = sch.shift if sch else None

    # факт: WorkLogDaily (log_loader пишет расширенное окно) или API
    from .debt_calculator import check_debt_for_log
    log = WorkLogDaily.objects.filter(operator=op, day=day).first()
    if log:
        full = log.full_duration or Z
        lock = log.lock_duration or Z
    else:
        full, lock = fetch_extended_fact(op, day, shift)
        log = WorkLogDaily(operator=op, day=day, shift=shift,
                           full_duration=full, lock_duration=lock)  # unsaved

    # Долг по КАНОНУ дневной нормы (тот же check_debt_for_log): учитывает
    # недоработку (wh), превышение перерыва (lock), послабление (grace) и
    # «доделал» (переработка компенсирует перерыв) — на расширенном факте.
    if shift:
        res = check_debt_for_log(log)
        debt_full = res["debt_full"]; debt_lock = res["debt_lock"]
        vtype = res["violation_type"]; calc_note = res["note"]
        norm_full = res["norm_full"]; norm_lock = res["norm_lock"]
    else:
        debt_full = debt_lock = norm_full = norm_lock = Z
        vtype = None; calc_note = "нет смены"

    # Параллельные льготы (Исключение/Обучение/Хоз — параллельны с учёбой по
    # матрице конфликтов) уменьшают дневную норму, как в debt_calculator.
    full_exempt, ben_partial = _benefit_exemption(op, day)
    ben_note = ""
    if full_exempt:
        debt_full = debt_lock = Z
        ben_note = "\nПолная льгота — день освобождён."
    elif ben_partial > Z and (debt_full + debt_lock) > Z:
        nf = max(debt_full - ben_partial, Z)
        rem = max(ben_partial - debt_full, Z)
        debt_lock = max(debt_lock - rem, Z)
        debt_full = nf
        ben_note = f"\nЛьготы −{ben_partial}."
    # пересчитать тип нарушения после льгот
    if debt_full > Z and debt_lock > Z:
        vtype = "both_violations"
    elif debt_full > Z:
        vtype = "insufficient_wh"
    elif debt_lock > Z:
        vtype = "exceeding_break"
    else:
        vtype = None
    calc_note = calc_note + ben_note

    debt = debt_full + debt_lock
    work_hours = max(full - lock, Z)

    # Идемпотентность БЕЗ churn: update_or_create — external_id (identity) barqaror
    # qoladi (delete+create har run yangi PK yasar edi → WFM'da dublikat).
    if debt > Z:
        WorkDebtDetail.objects.update_or_create(
            source="ucheba", source_object_id=tr.id, day=day,
            defaults=dict(
                operator=op, cycle=cycle, violation_type=vtype,
                shift=shift, shift_code_snapshot=(shift.code if shift else None),
                norm_full=norm_full, fact_full=full,
                debt_full=debt_full, norm_lock=norm_lock, fact_lock=lock,
                debt_lock=debt_lock,
                note=(f"Учёба #{tr.id} за {day} (расш. окно).\n{calc_note}"),
            ),
        )
        status = "partial" if work_hours > Z else "declined"
    else:
        # долга нет → прежнюю запись удаляем (этот delete нужно синхронизировать в WFM).
        WorkDebtDetail.objects.filter(
            source="ucheba", source_object_id=tr.id, day=day).delete()
        status = "approved"

    uday.fact_full = full; uday.fact_lock = lock; uday.debt = debt
    uday.status = status; uday.verified_at = now()
    uday.note = (f"{calc_note}\n→ долг {debt} (р/в {debt_full}, перерыв {debt_lock}).")
    uday.save(update_fields=["fact_full", "fact_lock", "debt", "status",
                             "verified_at", "note", "updated_at"])
    recompute_for_operator(op, cycle)
    return {"day": str(day), "status": status, "debt": str(debt),
            "debt_full": str(debt_full), "debt_lock": str(debt_lock),
            "work": str(work_hours)}


def verify_due_ucheba(target_date=None) -> dict:
    """
    Проверить дни учёбы, которые уже прошли (day <= target_date). После того
    как ВСЕ дни заявки проверены и end_date прошёл — Transfer → approved.
    Идемпотентно.
    """
    if target_date is None:
        target_date = localdate() - timedelta(days=1)

    stats = {"checked": 0, "approved": 0, "partial": 0, "declined": 0, "errors": 0,
             "requests_approved": 0}

    udays = (UchebaDay.objects.filter(day__lte=target_date)
             .exclude(transfer__status="declined")
             .select_related("transfer", "transfer__operator"))
    for uday in udays:
        try:
            # перепроверяем pending или ранее посчитанные (идемпотентно)
            res = verify_ucheba_day(uday)
            stats["checked"] += 1
            stats[res["status"]] = stats.get(res["status"], 0) + 1
        except Exception:  # noqa: BLE001
            stats["errors"] += 1

    # Заявки, где все дни проверены и end_date прошёл → approved
    due = (Transfer.objects.filter(type_rule__code=UCHEBA_CODE, status="pending")
           .annotate(last_day=Max("ucheba_days__day"))
           .filter(last_day__isnull=False, last_day__lte=target_date))
    for tr in due:
        if not tr.ucheba_days.filter(status="pending").exists():
            tr.status = "approved"
            tr.verified_at = now()
            tr.save(update_fields=["status", "verified_at", "updated_at"])
            stats["requests_approved"] += 1
    return stats
