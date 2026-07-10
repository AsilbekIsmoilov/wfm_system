"""
Сервис приёма заявок ОТПРАШИВАНИЯ (Transfer, тип «otprashivanie»).

Идея (Transfer = отгул, связанный Compensation(otrabotka) = отработка):
  - оператор отпрашивается на один/несколько РАБОЧИХ дней в окне времени
    (например 09:00–12:00) → длительность за день считается автоматически;
  - при приёме проверяются конфликты на дни отгула: Отработка (Compensation),
    Учёба в рабочее время (Transfer), Перенос рабочего дня (Transfer) →
    если есть, заявка не принимается;
  - оператор СРАЗУ указывает, когда отработает отпрошенные часы (как в
    отработке): дни отработки, общая длительность делится по ним поровну;
  - создаётся Transfer(otprashivanie) + дни отгула (OtprashivanieDay) и
    СВЯЗАННЫЙ Compensation(otrabotka) — план отработки БЕЗ записей долга (WDD);
  - долги (WDD source="time_off") за дни отгула создаёт дневной пайплайн
    (process_otprashivanie_debts, после debt_calculator) и ПРИВЯЗЫВАЕТ их к
    плану отработки. Дальше работает обычный движок отработки (verify, авто).
"""

from datetime import date, datetime, timedelta

from django.db import transaction
from django.utils.timezone import now

from ..models import (
    Compensation,
    CompensationDay,
    CompensationDebtLink,
    OperatorScheduleDay,
    Operator,
    RequestTypeRule,
    Transfer,
    WorkDebtDetail,
    OtprashivanieDay,
)
from . import notify
from .cycle import get_or_create_active_cycle, get_cycle_for_date
from .otrabotka import (
    split_duration, _normalize_days,
    get_conflicts as _otrabotka_conflicts,
    _otrabotka_occupied_days,
)
from .work_debt import recompute_for_date, recompute_for_operator

OTPR_CODE = "otprashivanie"
OTRABOTKA_CODE = "otrabotka"

ACTIVE_STATUSES = ["pending", "in_progress", "approved", "partial", "completed"]

# Конфликтующие с отпрашиванием заявки на дни отгула
CONFLICT_COMPENSATION_CODES = ["otrabotka"]
CONFLICT_TRANSFER_CODES = ["ucheba_rabochee", "perenos_dnya"]


class OtprashivanieError(Exception):
    """Бизнес-ошибка приёма заявки отпрашивания (мапится в HTTP 4xx)."""

    def __init__(self, message, code="error", payload=None, http_status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.payload = payload or {}
        self.http_status = http_status


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #

def _window_duration(hour_from, hour_to) -> timedelta:
    """Длительность окна hour_from–hour_to (через полночь → +24ч)."""
    a = datetime.combine(date.min, hour_from)
    b = datetime.combine(date.min, hour_to)
    d = b - a
    if d < timedelta(0):
        d += timedelta(days=1)
    return d


def _parse_time(v):
    if v is None or v == "":
        return None
    if hasattr(v, "hour"):
        return v
    parts = [int(x) for x in str(v).split(":")]
    while len(parts) < 2:
        parts.append(0)
    from datetime import time as _t
    return _t(parts[0], parts[1], parts[2] if len(parts) > 2 else 0)


def _is_working_day(operator, day) -> bool:
    sch = (
        OperatorScheduleDay.objects.filter(operator=operator, day=day)
        .select_related("shift").first()
    )
    return bool(sch and sch.shift_id and not sch.is_day_off)


def get_otprashivanie_conflicts(operator, leave_days) -> list:
    """Конфликты на дни отгула — ЕДИНАЯ матрица (services.conflicts):
    otrabotka / ucheba_rabochee / perenos_dnya."""
    from .conflicts import find_conflicts
    return find_conflicts(operator, "otprashivanie", leave_days)


# --------------------------------------------------------------------------- #
# Валидация
# --------------------------------------------------------------------------- #

def _validate_common(leave_days, hour_from, hour_to, workoff_days):
    """Глобальные проверки (не зависят от оператора). Возвращает нормализованные данные."""
    cycle = get_or_create_active_cycle()

    try:
        otpr_rule = RequestTypeRule.objects.get(
            code=OTPR_CODE, category="transfer", is_active=True)
        otr_rule = RequestTypeRule.objects.get(
            code=OTRABOTKA_CODE, category="compensation", is_active=True)
    except RequestTypeRule.DoesNotExist:
        raise OtprashivanieError(
            "Типы Отпрашивание / Отработка не настроены.",
            code="type_missing", http_status=500)

    hour_from = _parse_time(hour_from)
    hour_to = _parse_time(hour_to)
    if not hour_from or not hour_to:
        raise OtprashivanieError("Не указано окно времени (с/по).", code="no_hours")
    win = _window_duration(hour_from, hour_to)
    if win <= timedelta(0):
        raise OtprashivanieError("Окно времени должно быть больше нуля.", code="bad_hours")

    leave_days = _normalize_days(leave_days)
    workoff_days = _normalize_days(workoff_days)
    if not leave_days:
        raise OtprashivanieError("Не выбран ни один день отгула.", code="no_leave_days")
    if not workoff_days:
        raise OtprashivanieError("Не указаны дни отработки.", code="no_workoff_days")

    today = date.today()
    for d in leave_days:
        if not cycle.contains(d):
            raise OtprashivanieError(
                f"День отгула {d} вне активного цикла.", code="leave_out_of_cycle")
        if d < today:
            raise OtprashivanieError(
                f"Заявку на прошедший день отгула ({d}) подать нельзя.",
                code="leave_in_past")
    for d in workoff_days:
        if not cycle.contains(d):
            raise OtprashivanieError(
                f"День отработки {d} вне активного цикла.", code="workoff_out_of_cycle")
        if d < today:
            raise OtprashivanieError(
                f"Нельзя планировать отработку на прошедший день {d}.",
                code="workoff_in_past")

    overlap = sorted(set(leave_days) & set(workoff_days))
    if overlap:
        raise OtprashivanieError(
            f"Дни отработки не могут совпадать с днями отгула: "
            f"{', '.join(str(d) for d in overlap)}.",
            code="workoff_overlaps_leave",
            payload={"days": [str(d) for d in overlap]})

    return {
        "cycle": cycle, "otpr_rule": otpr_rule, "otr_rule": otr_rule,
        "hour_from": hour_from, "hour_to": hour_to, "window": win,
        "leave_days": leave_days, "workoff_days": workoff_days,
    }


def _validate_operator(operator, ctx):
    """Проверки для конкретного оператора. Возвращает (ok, reason|None)."""
    # дни отгула — только рабочие дни
    non_work = [str(d) for d in ctx["leave_days"] if not _is_working_day(operator, d)]
    if non_work:
        return False, {"code": "leave_not_working_day", "days": non_work}

    # запрет дубля: на эти дни уже есть отпрашивание (1 тип/1 опер./1 день — раз)
    dup = sorted(str(d) for d in OtprashivanieDay.objects.filter(
        transfer__operator=operator, transfer__type_rule__code=OTPR_CODE,
        transfer__status__in=ACTIVE_STATUSES, day__in=ctx["leave_days"],
    ).values_list("day", flat=True))
    if dup:
        return False, {"code": "duplicate_leave", "days": dup}

    # конфликты на дни отгула
    conflicts = get_otprashivanie_conflicts(operator, ctx["leave_days"])
    if conflicts:
        return False, {"code": "conflict", "conflicts": conflicts}

    # конфликты на дни отработки (otprashivanie/perenos — как у обычной отработки)
    otr_conf = _otrabotka_conflicts(operator, ctx["workoff_days"])
    if otr_conf:
        return False, {"code": "workoff_conflict", "conflicts": otr_conf}

    # один день отработки = одна отработка (как в accept_otrabotka): день
    # отработки не должен быть уже занят другой отработкой (в т.ч. планом
    # другого отпрашивания)
    occupied = _otrabotka_occupied_days(operator, ctx["cycle"])
    busy = sorted(str(d) for d in ctx["workoff_days"] if d in occupied)
    if busy:
        return False, {"code": "workoff_busy", "days": busy}

    return True, None


# --------------------------------------------------------------------------- #
# Preview / Accept
# --------------------------------------------------------------------------- #

def preview_otprashivanie(*, operators, leave_days, hour_from, hour_to,
                          workoff_days) -> dict:
    """Сухой прогон: длительности, распределение, конфликты по операторам."""
    ctx = _validate_common(leave_days, hour_from, hour_to, workoff_days)
    win = ctx["window"]
    total = win * len(ctx["leave_days"])
    parts = split_duration(total, len(ctx["workoff_days"]))

    per_op = []
    for op in operators:
        ok, reason = _validate_operator(op, ctx)
        per_op.append({
            "operator_id": op.id,
            "operator": op.full_name,
            "ok": ok,
            "reason": reason,
        })

    return {
        "window": str(win),
        "total_duration": str(total),
        "leave_days": [str(d) for d in ctx["leave_days"]],
        "workoff_plan": [
            {"day": str(d), "allocated_duration": str(p)}
            for d, p in zip(ctx["workoff_days"], parts)
        ],
        "operators": per_op,
        "ok": all(o["ok"] for o in per_op),
    }


def apply_otprashivanie_effects(tr, operator, ctx, comment=None, user=None):
    """Установить/пересоздать отгул (Transfer + OtprashivanieDay) И связанный план
    отработки (Compensation otrabotka pending + CompensationDay). Единый источник
    эффектов отпрашивания — приём (_accept_one) и редактирование (transfer_edit).
    `tr` содержит operator/cycle/type_rule; связанный comp переиспользуется, если
    уже есть (стабильный PK). Возвращает (tr, comp)."""
    cycle = ctx["cycle"]
    win = ctx["window"]
    leave_days = ctx["leave_days"]
    workoff_days = ctx["workoff_days"]
    total = win * len(leave_days)

    # 1) отгул Transfer (status approved — отгул разрешён)
    tr.status = "approved"
    tr.date_from = min(leave_days)
    tr.date_to = max(leave_days)
    tr.hour_from = ctx["hour_from"]
    tr.hour_to = ctx["hour_to"]
    tr.requested_duration = total
    tr.verified_duration = total
    tr.remaining_debt = total
    if comment is not None:
        tr.comment = comment
    tr.save()
    tr.otprashivanie_days.all().delete()
    for d in leave_days:
        OtprashivanieDay.objects.create(
            transfer=tr, day=d, hour_from=ctx["hour_from"],
            hour_to=ctx["hour_to"], duration=win,
        )

    # 2) связанный Compensation(otrabotka) — план отработки БЕЗ WDD (pending).
    #    Переиспользуем существующий comp при редактировании (PK стабилен).
    comp = tr.repayment_compensation
    if comp is None:
        comp = Compensation(
            operator=operator, cycle=cycle, type_rule=ctx["otr_rule"],
            source="system", fixed_by=user,
        )
    comp.status = "pending"
    comp.planned_date = min(workoff_days)
    comp.requested_duration = total
    comp.comment = f"Отработка отпрашивания (заявка #{tr.id})"
    comp.debts_snapshot = []
    comp.claim_metadata = {"otprashivanie_transfer_id": tr.id}
    comp.auto_check_result = {}
    comp.save()
    comp.days.all().delete()
    for d, p in zip(workoff_days, split_duration(total, len(workoff_days))):
        CompensationDay.objects.create(
            compensation=comp, day=d, allocated_duration=p, status="pending")

    if tr.repayment_compensation_id != comp.id:
        tr.repayment_compensation = comp
        tr.save(update_fields=["repayment_compensation", "updated_at"])
    return tr, comp


def revert_otprashivanie_effects(tr):
    """Откатить эффекты отпрашивания: удалить OtprashivanieDay отгула и
    CompensationDay связанного плана отработки. Записи tr и comp сохраняются
    (переиспортся в apply — стабильный PK). Идемпотентно."""
    tr.otprashivanie_days.all().delete()
    comp = tr.repayment_compensation
    if comp is not None:
        comp.days.all().delete()


@transaction.atomic
def _accept_one(operator, ctx, comment, user, source, pdf_file, screens):
    """Создать заявку отпрашивания + связанный план отработки для одного оператора."""
    tr = Transfer(
        operator=operator, cycle=ctx["cycle"], type_rule=ctx["otpr_rule"],
        comment=comment or "", pdf_file=pdf_file, screens=screens, fixed_by=user,
    )
    tr, comp = apply_otprashivanie_effects(tr, operator, ctx, comment=comment, user=user)

    notify.notify_operator(operator, "otprashivanie_created", {
        "transfer_id": tr.id,
        "compensation_id": comp.id,
        "total_duration": str(tr.requested_duration),
        "leave_days": [str(d) for d in ctx["leave_days"]],
        "workoff_days": [str(d) for d in ctx["workoff_days"]],
    })
    return tr


def accept_otprashivanie(*, operators, leave_days, hour_from, hour_to,
                         workoff_days, comment="", user=None,
                         source="requested", pdf_file=None, screens=None) -> dict:
    """
    Принять заявку(и) отпрашивания. Для нескольких операторов — по одной на
    каждого; оператор с конфликтом/не-рабочим днём пропускается и попадает в
    skipped (батч не падает целиком).
    """
    ctx = _validate_common(leave_days, hour_from, hour_to, workoff_days)

    created, skipped = [], []
    for op in operators:
        ok, reason = _validate_operator(op, ctx)
        if not ok:
            skipped.append({"operator_id": op.id, "operator": op.full_name,
                            "reason": reason})
            continue
        tr = _accept_one(op, ctx, comment, user, source, pdf_file, screens)
        created.append({"operator_id": op.id, "operator": op.full_name,
                        "transfer_id": tr.id,
                        "compensation_id": tr.repayment_compensation_id})

    if not created and skipped:
        # все пропущены — вернуть ошибку с деталями
        raise OtprashivanieError(
            "Ни одна заявка не принята (конфликты / не рабочие дни).",
            code="all_skipped", payload={"skipped": skipped}, http_status=409)

    return {"created": created, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Дневной шаг: создать долги за отгул и привязать к плану отработки
# --------------------------------------------------------------------------- #

@transaction.atomic
def process_otprashivanie_debts(for_date) -> dict:
    """
    Для дней отгула, приходящихся на for_date: перенести дневную недоработку
    (созданную debt_calculator как source="shift") в долг source="time_off",
    привязать к плану отработки (CompensationDebtLink) и заблокировать.

    Сумма time_off ограничена отпрошенными часами за день (od.duration);
    превышение остаётся обычным долгом (source="shift").

    ВАЖНО: вызывать ПОСЛЕ debt_calculator.calculate_work_debts(for_date) в
    дневном пайплайне. Идемпотентно (откатывает свои прежние time_off).
    """
    Z = timedelta(0)
    cycle = get_cycle_for_date(for_date) or get_or_create_active_cycle()

    odays = (
        OtprashivanieDay.objects.filter(day=for_date)
        .exclude(transfer__status="declined")
        .select_related("transfer", "transfer__operator",
                        "transfer__repayment_compensation")
    )
    stats = {"processed": 0, "no_debt": 0}

    for od in odays:
        tr = od.transfer
        op = tr.operator
        comp = tr.repayment_compensation

        # 1) откат прежнего результата для (заявка, день)
        prior = WorkDebtDetail.objects.filter(
            source="time_off", source_object_id=tr.id, day=for_date)
        CompensationDebtLink.objects.filter(debt_detail__in=prior).delete()
        prior.delete()

        # 2) свежая недоработка дня от debt_calculator
        shift_qs = WorkDebtDetail.objects.filter(
            operator=op, day=for_date, source="shift",
            locked_for_compensation=False)
        sf_full = sum((w.debt_full or Z for w in shift_qs), Z)
        sf_lock = sum((w.debt_lock or Z for w in shift_qs), Z)
        sc = next((w.shift_code_snapshot for w in shift_qs if w.shift_code_snapshot), None)
        if (sf_full + sf_lock) <= Z:
            stats["no_debt"] += 1
            continue

        # 3) ограничить отпрошенными часами; недоработка сверх отгула — обычный долг
        leave = od.duration or Z
        to_full = min(sf_full, leave)
        rem = leave - to_full
        to_lock = min(sf_lock, rem)
        left_full = sf_full - to_full
        left_lock = sf_lock - to_lock

        # 4) заменить shift-WDD: удалить, пересоздать остаток-нарушение (если есть)
        shift_qs.delete()
        if left_full > Z or left_lock > Z:
            WorkDebtDetail.objects.create(
                operator=op, day=for_date, cycle=cycle, source="shift",
                shift_code_snapshot=sc,
                violation_type=("both_violations" if left_full > Z and left_lock > Z
                                else "exceeding_break" if left_lock > Z
                                else "insufficient_wh"),
                debt_full=left_full, debt_lock=left_lock,
                note=(f"Недоработка сверх отпрашивания #{tr.id} за {for_date}."),
            )

        # 5) создать долг time_off + привязать к плану отработки
        if to_full > Z or to_lock > Z:
            wdd = WorkDebtDetail.objects.create(
                operator=op, day=for_date, cycle=cycle, source="time_off",
                source_object_id=tr.id, shift_code_snapshot=sc,
                violation_type=("both_violations" if to_full > Z and to_lock > Z
                                else "exceeding_break" if to_lock > Z
                                else "insufficient_wh"),
                debt_full=to_full, debt_lock=to_lock,
                locked_for_compensation=True,
                note=(f"Отпрашивание #{tr.id} за {for_date} "
                      f"({od.hour_from:%H:%M}–{od.hour_to:%H:%M}).\n"
                      f"Отрабатывается планом #{comp.id if comp else '—'}."),
            )
            if comp:
                CompensationDebtLink.objects.create(
                    compensation=comp, debt_detail=wdd, applied=False,
                    snapshot={
                        "id": wdd.id, "day": str(for_date), "source": "time_off",
                        "debt_full": str(to_full), "debt_lock": str(to_lock),
                        "transfer_id": tr.id,
                    },
                )
            stats["processed"] += 1

    recompute_for_date(for_date)
    return stats
