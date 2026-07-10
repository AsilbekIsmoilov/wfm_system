"""
Сервис приёма заявок-ЛЬГОТ/ОСВОБОЖДЕНИЙ (Transfer, benefit-типы):
  Исключение, Обучение, Льгота (+ все подтипы льгот), Хозяйственные работы.

Назначение: когда у оператора есть льгота/привилегия (от компании) и она
попадает на РАБОЧИЙ день — по желанию оператора льгота применяется, рабочий
день (или часть часов) за этот день отменяется, и долг за этот день/часы НЕ
начисляется. Только рабочие дни (в выходной — нельзя).

Списание долга уже реализовано в debt_calculator: эти типы имеют
`exempts_from_daily_debt=True` → полный день (без часов) освобождает день
целиком, с часами — уменьшает норму на эти часы. Здесь — ПРИЁМ заявки.

Подтипы «Прочие льготы» (lgota_prochie):
  - otgul (Отгул) — полный день;
  - korotkiy_den (Короткий день) — часть часов.

Конфликты: только «Хозяйственные работы» конфликтует с «Перенос рабочего
дня» (perenos_dnya). Остальные (Исключение/Обучение/Льготы) — параллельно
допускаются с любыми заявками.
"""

from datetime import date, timedelta

from django.db import transaction

from ..models import (
    BenefitDay,
    Operator,
    OperatorScheduleDay,
    RequestTypeRule,
    Transfer,
)
from . import notify
from .cycle import get_or_create_active_cycle
from .otprashivanie import _window_duration, _parse_time, _is_working_day

ACTIVE_STATUSES = ["pending", "in_progress", "approved", "partial", "completed"]

BENEFIT_CODES = [
    "isklyuchenie", "obuchenie",
    "lgota", "lgota_brak", "lgota_rojdenie", "lgota_utrata",
    "lgota_pereezd", "lgota_prochie",
    "hoz_raboty",
    "perenos_dnya",   # Перенос рабочего дня — приём/редактирование как у льготы
]

# Конфликтующие заявки по коду льготы (на те же дни). Если есть — не принимаем.
CONFLICT_MAP = {
    "hoz_raboty": ["perenos_dnya"],
}

PROCHIE_SUBTYPES = ["otgul", "korotkiy_den"]

# «все операторы 9ч смены» = ВСЕ смены КРОМЕ 12-часовых.
TWELVE_HOURS = timedelta(hours=12)


class BenefitError(Exception):
    def __init__(self, message, code="error", payload=None, http_status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.payload = payload or {}
        self.http_status = http_status


# --------------------------------------------------------------------------- #
# Подбор операторов
# --------------------------------------------------------------------------- #

def resolve_operators(operator_ids, select_all_shift, days) -> list:
    """
    Операторы: явный список ИЛИ «вся 9ч смена» = ВСЕ операторы со сменой на
    выбранные дни, КРОМЕ 12-часовых смен (08-20 / 20-08).
    """
    if select_all_shift:
        if select_all_shift != "9h":
            raise BenefitError(
                "Поддерживается только «вся смена (кроме 12ч)».",
                code="bad_shift_group")
        op_ids = (
            OperatorScheduleDay.objects.filter(
                day__in=days, is_day_off=False, shift__isnull=False,
            )
            .exclude(shift__norm_full=TWELVE_HOURS)
            .values_list("operator_id", flat=True).distinct()
        )
        return list(Operator.objects.filter(id__in=op_ids, is_active=True))

    return list(Operator.objects.filter(id__in=(operator_ids or []), is_active=True))


# --------------------------------------------------------------------------- #
# Конфликты
# --------------------------------------------------------------------------- #

def get_benefit_conflicts(operator, code, days) -> list:
    # ЕДИНАЯ матрица конфликтов (services.conflicts):
    #   lgota* → Учёба + Перенос; hoz → Перенос; isklyuchenie/obuchenie → нет.
    from .conflicts import find_conflicts
    return find_conflicts(operator, code, days)


# --------------------------------------------------------------------------- #
# Валидация
# --------------------------------------------------------------------------- #

def _normalize_days(days):
    out, seen = [], set()
    for d in days:
        if isinstance(d, str):
            d = date.fromisoformat(d)
        if d not in seen:
            seen.add(d)
            out.append(d)
    return sorted(out)


def _validate_common(code, subtype, days, hour_from, hour_to):
    cycle = get_or_create_active_cycle()

    if code not in BENEFIT_CODES:
        raise BenefitError(
            f"Тип «{code}» не относится к льготам.", code="not_benefit")
    try:
        rule = RequestTypeRule.objects.get(
            code=code, category="transfer", is_active=True)
    except RequestTypeRule.DoesNotExist:
        raise BenefitError(
            f"«{code}» (Трансфер) не найден или неактивен.",
            code="type_missing", http_status=404)

    days = _normalize_days(days)
    if not days:
        raise BenefitError("Не выбран ни один день.", code="no_days")
    # ЕДИНАЯ проверка дат: в цикле + не в прошлом (ретро для льгот нет).
    from .cycle import check_request_days
    bad = check_request_days(days, allow_past=False, cycle=cycle)
    if bad:
        b = bad[0]
        if b["code"] == "out_of_cycle":
            raise BenefitError(f"День {b['day']} вне активного цикла ({b['cycle']}).",
                               code="day_out_of_cycle", payload={"violations": bad})
        raise BenefitError(
            f"Заявку на прошедший день ({b['day']}) подать нельзя.",
            code="day_in_past", payload={"violations": bad})

    # подтип — только для «Прочие льготы»
    if code == "lgota_prochie":
        if subtype not in PROCHIE_SUBTYPES:
            raise BenefitError(
                "Для «Прочие льготы» нужен подтип: Отгул или Короткий день.",
                code="subtype_required")
    else:
        subtype = None

    hour_from = _parse_time(hour_from)
    hour_to = _parse_time(hour_to)

    # Полный день: подтип otgul, ИЛИ часы не заданы.
    # Короткий день / частичная льгота: нужны часы.
    if subtype == "otgul":
        full_day, window = True, None
    elif subtype == "korotkiy_den":
        if not (hour_from and hour_to):
            raise BenefitError(
                "Для «Короткий день» нужно окно часов.", code="hours_required")
        full_day, window = False, _window_duration(hour_from, hour_to)
    elif hour_from and hour_to:
        full_day, window = False, _window_duration(hour_from, hour_to)
    else:
        full_day, window = True, None  # весь день

    if not full_day and window <= timedelta(0):
        raise BenefitError("Окно часов должно быть больше нуля.", code="bad_hours")

    return {
        "cycle": cycle, "rule": rule, "days": days, "subtype": subtype,
        "full_day": full_day, "window": window,
        "hour_from": hour_from, "hour_to": hour_to,
    }


def _work_norm(shift) -> timedelta:
    """Норма рабочих часов (wh) смены: norm_full − lock_norm (7:30/6:30/9:40)."""
    Z = timedelta(0)
    if shift.norm_lock_warn_at and shift.norm_lock_warn_at > Z:   # 9ч/8ч
        lock_norm = shift.norm_lock_warn_at
    else:                                                          # 12ч
        lock_norm = shift.norm_lock_soft_cap or Z
    return max((shift.norm_full or Z) - lock_norm, Z)


def _day_excused(operator, day, ctx) -> timedelta:
    """Сколько времени освобождается за день: окно (частично) или work_norm (весь день)."""
    if not ctx["full_day"]:
        return ctx["window"] or timedelta(0)
    sch = (OperatorScheduleDay.objects.filter(operator=operator, day=day)
           .select_related("shift").first())
    if sch and sch.shift:
        return _work_norm(sch.shift)
    return timedelta(0)


def _taken_days(operator, rule, days, exclude_transfer_id=None) -> set:
    """Дни, на которые у оператора УЖЕ есть АКТИВНАЯ заявка ЭТОГО ЖЕ типа
    (запрет дубля: 1 тип / 1 оператор / 1 день — один раз).
    exclude_transfer_id — при редактировании исключает саму редактируемую
    заявку, иначе она сочла бы собственные дни занятыми."""
    bday_qs = BenefitDay.objects.filter(
        transfer__operator=operator, transfer__type_rule=rule,
        transfer__status__in=ACTIVE_STATUSES, day__in=days,
    )
    if exclude_transfer_id is not None:
        bday_qs = bday_qs.exclude(transfer_id=exclude_transfer_id)
    taken = set(bday_qs.values_list("day", flat=True))
    # старые заявки без BenefitDay (по диапазону дат)
    lo, hi = min(days), max(days)
    range_qs = Transfer.objects.filter(
        operator=operator, type_rule=rule, status__in=ACTIVE_STATUSES,
        benefit_days__isnull=True, date_from__lte=hi, date_to__gte=lo,
    )
    if exclude_transfer_id is not None:
        range_qs = range_qs.exclude(id=exclude_transfer_id)
    for t in range_qs.distinct():
        d, end = t.date_from, (t.date_to or t.date_from)
        while d <= end:
            if d in days:
                taken.add(d)
            d += timedelta(days=1)
    return taken


def _operator_plan(operator, ctx, exclude_transfer_id=None):
    """
    (valid_days, dropped) для оператора. valid_days — РАБОЧИЕ дни БЕЗ
    конфликтов и БЕЗ дубля. Остальные дни выбрасываются с причиной.
    exclude_transfer_id — при редактировании исключает саму заявку из проверки
    дубля (её дни не считаются занятыми ею же).
    """
    days = ctx["days"]
    non_work = [d for d in days if not _is_working_day(operator, d)]
    conflicts = get_benefit_conflicts(operator, ctx["rule"].code, days)
    conf_days = {c["day"] for c in conflicts}   # строки
    taken = _taken_days(operator, ctx["rule"], days, exclude_transfer_id)
    nonwork_set = set(non_work)
    valid = [d for d in days
             if d not in nonwork_set and str(d) not in conf_days and d not in taken]
    dropped = {
        "not_working_day": [str(d) for d in non_work],
        "conflicts": conflicts,
        "duplicate": sorted(str(d) for d in taken),
    }
    return valid, dropped


# --------------------------------------------------------------------------- #
# Preview / Accept
# --------------------------------------------------------------------------- #

def preview_benefit(*, operators=None, operator_ids=None, select_all_shift=None,
                    code, subtype=None, days, hour_from=None, hour_to=None) -> dict:
    ctx = _validate_common(code, subtype, days, hour_from, hour_to)
    ops = operators if operators is not None else resolve_operators(
        operator_ids, select_all_shift, ctx["days"])

    per_op = []
    for op in ops:
        valid, dropped = _operator_plan(op, ctx)
        per_op.append({
            "operator_id": op.id, "operator": op.full_name,
            "ok": bool(valid),
            "valid_days": [str(d) for d in valid],
            "dropped": dropped,
        })

    return {
        "type": {"code": ctx["rule"].code, "display_name": ctx["rule"].display_name},
        "subtype": ctx["subtype"],
        "full_day": ctx["full_day"],
        "per_day_duration": "весь день" if ctx["full_day"] else str(ctx["window"]),
        "days": [str(d) for d in ctx["days"]],
        "operators": per_op,
        "resolved_count": len(ops),
        "ok": bool(per_op) and all(o["ok"] for o in per_op),
    }


def apply_benefit_effects(tr, operator, valid_days, ctx):
    """Установить поля льготы на tr (status/даты/часы/длительность/подтип) и
    пересоздать BenefitDay. Единый источник эффектов льготы — используется при
    приёме (_accept_request) и редактировании (transfer_edit). `tr` уже содержит
    operator/cycle/type_rule; может быть новым (несохранённым) или существующим.
    Льгота авто-одобрена → Подтверждённая = Запрошенная."""
    total = sum((_day_excused(operator, d, ctx) for d in valid_days), timedelta(0))
    hf = None if ctx["full_day"] else ctx["hour_from"]
    ht = None if ctx["full_day"] else ctx["hour_to"]
    tr.status = "approved"
    tr.date_from = min(valid_days)
    tr.date_to = max(valid_days)
    tr.hour_from = hf
    tr.hour_to = ht
    tr.requested_duration = total or None
    tr.verified_duration = total or None
    tr.subtype = ctx["subtype"]
    tr.save()
    tr.benefit_days.all().delete()
    for d in valid_days:
        BenefitDay.objects.create(
            transfer=tr, day=d, hour_from=hf, hour_to=ht,
            duration=None if ctx["full_day"] else ctx["window"],
        )
    return tr


def revert_benefit_effects(tr):
    """Удалить BenefitDay заявки (поля tr перезапишет apply). Идемпотентно."""
    tr.benefit_days.all().delete()


@transaction.atomic
def _accept_request(operator, valid_days, ctx, comment, user, source, pdf_file, screens):
    """ОДНА заявка-льгота на оператора, покрывающая все его рабочие дни."""
    tr = Transfer(
        operator=operator, cycle=ctx["cycle"], type_rule=ctx["rule"],
        comment=comment or "", pdf_file=pdf_file, screens=screens, fixed_by=user,
    )
    return apply_benefit_effects(tr, operator, valid_days, ctx)


def accept_benefit(*, operators=None, operator_ids=None, select_all_shift=None,
                   code, subtype=None, days, hour_from=None, hour_to=None,
                   comment="", user=None, source="requested",
                   pdf_file=None, screens=None) -> dict:
    """
    Принять заявку-льготу: ОДНА заявка на оператора со всеми его рабочими днями
    (через BenefitDay; непоследовательные дни поддержаны). Выходные/конфликтные
    дни выбрасываются; если у оператора не осталось дней — он пропускается.
    """
    ctx = _validate_common(code, subtype, days, hour_from, hour_to)
    ops = operators if operators is not None else resolve_operators(
        operator_ids, select_all_shift, ctx["days"])
    if not ops:
        raise BenefitError("Не найдено ни одного оператора.", code="no_operators")

    created, skipped = [], []
    for op in ops:
        valid, dropped = _operator_plan(op, ctx)
        if not valid:
            skipped.append({"operator_id": op.id, "operator": op.full_name,
                            "reason": {"code": "no_valid_days", **dropped}})
            continue
        tr = _accept_request(op, valid, ctx, comment, user, source, pdf_file, screens)
        created.append({"operator_id": op.id, "operator": op.full_name,
                        "transfer_id": tr.id,
                        "days": [str(d) for d in valid], "dropped": dropped})
        notify.notify_operator(op, "benefit_created", {
            "code": ctx["rule"].code, "subtype": ctx["subtype"],
            "days": [str(d) for d in valid], "full_day": ctx["full_day"],
        })

    if not created and skipped:
        raise BenefitError(
            "Ни одна заявка не принята (выходные дни / конфликты).",
            code="all_skipped", payload={"skipped": skipped}, http_status=409)

    return {"created": created, "skipped": skipped}
