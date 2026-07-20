"""
Сервис приёма заявок отработки (Compensation, тип «otrabotka»).

Фаза 1 — приём заявки:
  - выбор оператора + его записей долга (WorkDebtDetail);
  - requested_duration = сумма выбранных долгов;
  - один или несколько дней (в пределах активного цикла), длительность
    делится по дням поровну;
  - проверка конфликтов: на выбранные дни не должно быть заявок
    «otprashivanie» / «perenos_dnya» (иначе заявка не принимается);
  - параллельно с отработкой допускаются: isklyuchenie, ucheba,
    obuchenie, льготы, xoz_raboty;
  - при приёме записи долга «переезжают» в заявку (CompensationDebtLink
    со снимком) и пропадают из списка открытых долгов
    (locked_for_compensation=True); current_debt пока НЕ уменьшается
    (статус pending), списание произойдёт в Фазе 2 при approved/partial.

Фаза 2 (расчёт approved/partial/declined по факту из API) — отдельно.
"""

from datetime import date, timedelta

from django.db import transaction
from django.utils.timezone import now

from ..models import (
    Compensation,
    CompensationDay,
    CompensationDebtLink,
    RequestTypeRule,
    Transfer,
    WorkDebtDetail,
)
from ..utils import strip_compensation_note
from . import notify
from .cycle import get_or_create_active_cycle

OTRABOTKA_CODE = "otrabotka"

# Типы переносов, которые КОНФЛИКТУЮТ с отработкой (нельзя на те же дни)
CONFLICT_TRANSFER_CODES = ["otprashivanie", "perenos_dnya"]

# Типы, допустимые ПАРАЛЛЕЛЬНО с отработкой (для справки/Фазы 2)
PARALLEL_TRANSFER_CODES = [
    "isklyuchenie", "ucheba_rabochee", "obuchenie",
    "lgota", "lgota_brak", "lgota_rojdenie", "lgota_utrata",
    "lgota_pereezd", "lgota_prochie", "hoz_raboty",
]

ACTIVE_STATUSES = ["pending", "in_progress", "approved", "partial"]


class OtrabotkaError(Exception):
    """Бизнес-ошибка приёма заявки отработки (мапится в HTTP 4xx)."""

    def __init__(self, message: str, code: str = "error",
                 payload: dict | None = None, http_status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.payload = payload or {}
        self.http_status = http_status


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #

def split_duration(total: timedelta, n: int) -> list[timedelta]:
    """
    Поделить total на n частей поровну (по секундам).
    Остаток (в секундах) раскидывается по первым дням.
    """
    if n <= 0:
        return []
    total_sec = int(total.total_seconds())
    base = total_sec // n
    rem = total_sec % n
    return [timedelta(seconds=base + (1 if i < rem else 0)) for i in range(n)]


def _normalize_days(days) -> list[date]:
    """Привести к отсортированному списку уникальных date."""
    out = []
    seen = set()
    for d in days:
        if isinstance(d, str):
            d = date.fromisoformat(d)
        if d not in seen:
            seen.add(d)
            out.append(d)
    return sorted(out)


def get_conflicts(operator, days: list[date]) -> list[dict]:
    """
    Вернуть конфликтующие заявки (otprashivanie/perenos_dnya),
    пересекающиеся с указанными днями.
    """
    if not days:
        return []
    day_min, day_max = min(days), max(days)
    day_set = set(days)

    transfers = (
        Transfer.objects.filter(
            operator=operator,
            type_rule__code__in=CONFLICT_TRANSFER_CODES,
            status__in=ACTIVE_STATUSES,
        )
        .select_related("type_rule")
    )

    conflicts = []
    for t in transfers:
        d_from = t.date_from
        d_to = t.date_to or t.date_from
        if not d_from:
            continue
        if d_to < day_min or d_from > day_max:
            continue
        # какие из выбранных дней попадают в диапазон заявки
        hit = sorted(
            str(d) for d in day_set if d_from <= d <= d_to
        )
        if hit:
            conflicts.append({
                "transfer_id": t.id,
                "code": t.type_rule.code,
                "display_name": t.type_rule.display_name,
                "status": t.status,
                "date_from": str(d_from),
                "date_to": str(d_to),
                "conflicting_days": hit,
            })
    return conflicts


def _otrabotka_occupied_days(operator, cycle) -> set[date]:
    """Дни, уже занятые днями отработки оператора в цикле (не declined)."""
    occupied = (
        CompensationDay.objects.filter(
            compensation__operator=operator,
            compensation__cycle=cycle,
            compensation__type_rule__code=OTRABOTKA_CODE,
        )
        .exclude(status="declined")
        .exclude(compensation__status="declined")
        .values_list("day", flat=True)
    )
    return set(occupied)


def _conflict_transfer_days(operator, cycle) -> set[date]:
    """Дни цикла, занятые конфликтующими заявками otprashivanie/perenos_dnya."""
    days: set[date] = set()
    transfers = Transfer.objects.filter(
        operator=operator,
        type_rule__code__in=CONFLICT_TRANSFER_CODES,
        status__in=ACTIVE_STATUSES,
    )
    for t in transfers:
        d_from = t.date_from
        d_to = t.date_to or t.date_from
        if not d_from:
            continue
        d = max(d_from, cycle.start_date)
        end = min(d_to, cycle.end_date)
        while d <= end:
            days.add(d)
            d += timedelta(days=1)
    return days


def get_disabled_days(operator, cycle) -> list[str]:
    """
    Дни, недоступные для новой заявки отработки (для календаря фронта):
      - уже занятые днями отработки (CompensationDay, не declined);
      - занятые конфликтующими заявками otprashivanie/perenos_dnya.
    """
    disabled = _otrabotka_occupied_days(operator, cycle)
    disabled |= _conflict_transfer_days(operator, cycle)
    return sorted(str(d) for d in disabled)


# --------------------------------------------------------------------------- #
# Основной приём
# --------------------------------------------------------------------------- #

def _validate_inputs(operator, debt_detail_ids, days, retroactive=False):
    """
    Общие проверки для preview и accept. Возвращает (cycle, rule, wdds, days, total).

    retroactive=False (обычная): дни должны быть СЕГОДНЯ или в будущем
        (оператор отработает позже; проверка — авто после прохождения дня).
    retroactive=True (ретроактивная): дни должны быть в прошлом/сегодня —
        оператор УЖЕ отработал, факт уже есть; проверка выполняется сразу.
    """
    cycle = get_or_create_active_cycle()

    try:
        rule = RequestTypeRule.objects.get(
            code=OTRABOTKA_CODE, category="compensation", is_active=True,
        )
    except RequestTypeRule.DoesNotExist:
        raise OtrabotkaError(
            "Тип «Отработка» не найден или неактивен.",
            code="type_missing", http_status=500,
        )

    days = _normalize_days(days)
    if not days:
        raise OtrabotkaError("Не выбран ни один день.", code="no_days")

    today = date.today()
    for d in days:
        if not cycle.contains(d):
            raise OtrabotkaError(
                f"День {d} вне активного цикла "
                f"({cycle.start_date}–{cycle.end_date}).",
                code="day_out_of_cycle",
            )
        if not retroactive and d < today:
            raise OtrabotkaError(
                f"Нельзя планировать отработку на прошедший день {d}. "
                "Для уже отработанного дня используйте ретроактивную заявку.",
                code="day_in_past",
            )
        if retroactive and d > today:
            raise OtrabotkaError(
                f"Ретроактивная отработка возможна только для прошедших "
                f"или сегодняшнего дня (день {d} — в будущем).",
                code="day_in_future",
            )

    # дни, уже занятые ДРУГОЙ заявкой отработки (конфликты переносов —
    # отдельно, с уведомлением, в get_conflicts/accept)
    occupied = _otrabotka_occupied_days(operator, cycle)
    busy = sorted(str(d) for d in days if d in occupied)
    if busy:
        raise OtrabotkaError(
            f"Дни уже заняты другой заявкой отработки: {', '.join(busy)}.",
            code="days_busy", payload={"busy_days": busy},
        )

    if not debt_detail_ids:
        raise OtrabotkaError("Не выбрана ни одна запись долга.", code="no_debts")

    wdds = list(
        WorkDebtDetail.objects.filter(id__in=debt_detail_ids, operator=operator)
    )
    found_ids = {w.id for w in wdds}
    missing = [i for i in debt_detail_ids if i not in found_ids]
    if missing:
        raise OtrabotkaError(
            f"Записи долга не найдены у оператора: {missing}.",
            code="debts_not_found", payload={"missing": missing},
        )

    already_locked = [w.id for w in wdds if w.locked_for_compensation]
    if already_locked:
        raise OtrabotkaError(
            f"Записи долга уже привязаны к другой заявке: {already_locked}.",
            code="debts_locked", payload={"locked": already_locked},
        )

    total = timedelta(0)
    for w in wdds:
        total += (w.debt_full or timedelta(0)) + (w.debt_lock or timedelta(0))
    if total <= timedelta(0):
        raise OtrabotkaError(
            "Сумма выбранных долгов равна нулю.", code="zero_debt",
        )

    return cycle, rule, wdds, days, total


def apply_otrabotka_effects(comp, wdds, days, day_durations):
    """Создать CompensationDay (по одному на день, со своей длительностью) +
    CompensationDebtLink и заблокировать выбранные WDD под заявку отработки.
    Единственный источник эффектов отработки — приём (accept_otrabotka) и
    редактирование (compensation_edit). `day_durations` — список timedelta,
    выровненный с `days` (каждый день редактируется отдельно на фронте).
    requested_duration заявки = их сумма (её выставляет вызывающий accept/edit)."""
    for d, p in zip(days, day_durations):
        CompensationDay.objects.create(
            compensation=comp, day=d, allocated_duration=p, status="pending",
        )

    # «Переезд» долгов в заявку + блокировка из списка открытых долгов
    snapshots = []
    for w in wdds:
        snap = {
            "id": w.id,
            "day": str(w.day),
            "source": w.source,
            "shift_code": w.shift_code_snapshot,
            "norm_full": str(w.norm_full),
            "fact_full": str(w.fact_full),
            "debt_full": str(w.debt_full),
            "norm_lock": str(w.norm_lock),
            "fact_lock": str(w.fact_lock),
            "debt_lock": str(w.debt_lock),
            "note": w.note or "",
        }
        CompensationDebtLink.objects.create(
            compensation=comp, debt_detail=w, snapshot=snap, applied=False,
        )
        snapshots.append(snap)

        w.locked_for_compensation = True
        w.note = (w.note or "") + (
            f"\nПеренесён в заявку отработки #{comp.id} ({now():%Y-%m-%d %H:%M})"
        )
        w.save(update_fields=["locked_for_compensation", "note", "updated_at"])

    comp.debts_snapshot = snapshots
    comp.save(update_fields=["debts_snapshot", "updated_at"])


def revert_otrabotka_effects(comp):
    """Откатить эффекты отработки: удалить CompensationDay и CompensationDebtLink
    этой заявки, разблокировать связанные WDD. Статус НЕ трогаем — им управляет
    вызывающий код. Идемпотентно (можно вызвать повторно)."""
    CompensationDay.objects.filter(compensation=comp).delete()
    for link in CompensationDebtLink.objects.filter(
        compensation=comp,
    ).select_related("debt_detail"):
        w = link.debt_detail
        if w:
            fields = []
            if w.locked_for_compensation:
                w.locked_for_compensation = False
                fields.append("locked_for_compensation")
            # Снять и пометку, которую поставило применение — иначе каждая
            # правка (откат + повторное применение) дописывала бы новую строку.
            cleaned = strip_compensation_note(w.note, comp.id)
            if cleaned != (w.note or ""):
                w.note = cleaned
                fields.append("note")
            if fields:
                w.save(update_fields=[*fields, "updated_at"])
        link.delete()
    if comp.debts_snapshot:
        comp.debts_snapshot = []
        comp.save(update_fields=["debts_snapshot", "updated_at"])


def preview_otrabotka(*, operator, debt_detail_ids, days, retroactive=False) -> dict:
    """
    Сухой прогон: проверки + конфликты + расчёт распределения по дням.
    Ничего не пишет в БД. Используется для шага подтверждения (ha/yo'q).
    """
    cycle, rule, wdds, days, total = _validate_inputs(
        operator, debt_detail_ids, days, retroactive=retroactive,
    )
    conflicts = get_conflicts(operator, days)
    parts = split_duration(total, len(days))
    per_day = [
        {"day": str(d), "allocated_duration": str(p)}
        for d, p in zip(days, parts)
    ]

    result = {
        "ok": not conflicts,
        "operator_id": operator.id,
        "total_duration": str(total),
        "days": per_day,
        "conflicts": conflicts,
    }
    if conflicts:
        result["message"] = (
            "На выбранные дни есть заявки Отпрашивание или Перенос "
            "рабочего дня — отработку принять нельзя."
        )
        notify.notify_operator(operator, "otrabotka_conflict", {
            "conflicts": conflicts, "days": [str(d) for d in days],
        })
    else:
        result["confirm_prompt"] = (
            f"Согласны отработать {total} в дни "
            f"{', '.join(str(d) for d in days)}?"
        )
        notify.notify_operator(operator, "otrabotka_confirm", {
            "total_duration": str(total),
            "days": per_day,
        })
    return result


def _parse_td(value):
    """timedelta | 'H:MM[:SS]' | секунды(int/str) → timedelta."""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(seconds=int(value))
    s = str(value).strip()
    if ":" in s:
        parts = [int(p) for p in s.split(":")]
        while len(parts) < 3:
            parts.append(0)
        return timedelta(hours=parts[0], minutes=parts[1], seconds=parts[2])
    return timedelta(seconds=int(s))


def _resolve_day_durations(durations, days, total):
    """Длительности отработки ПО ДНЯМ (каждый день редактируется на фронте).
    None/пусто → авто-раздел суммы долгов по дням (split_duration, обычная отработка).
    Иначе: len(durations)==len(days), каждая > 0, а СУММА ≤ сумма выбранных долгов
    (частичная отработка). Недопокрытый остаток при верификации возвращается в
    открытый долг (_apply_credit_partial). Возвращает (list[timedelta], requested)."""
    if durations in (None, "", []):
        parts = split_duration(total, len(days))
        return parts, sum(parts, timedelta(0))
    if len(durations) != len(days):
        raise OtrabotkaError(
            f"Число длительностей ({len(durations)}) не совпадает с числом "
            f"дней ({len(days)}).",
            code="durations_mismatch",
            payload={"durations": len(durations), "days": len(days)})
    parsed = []
    for v in durations:
        td = _parse_td(v)
        if td <= timedelta(0):
            raise OtrabotkaError(
                "Длительность отработки по дню должна быть больше нуля.",
                code="bad_duration")
        parsed.append(td)
    requested = sum(parsed, timedelta(0))
    if requested > total:
        raise OtrabotkaError(
            f"Суммарная длительность отработки ({requested}) больше суммы "
            f"выбранных долгов ({total}).",
            code="durations_too_big",
            payload={"requested": str(requested), "total": str(total)})
    return parsed, requested


@transaction.atomic
def accept_otrabotka(*, operator, debt_detail_ids, days, durations=None,
                     comment="", user=None, source="requested",
                     pdf_file=None, screens=None,
                     retroactive=False) -> Compensation:
    """
    Принять заявку отработки. Конфликты => OtrabotkaError (не принимается).

    durations — необязательный список длительностей ПО ДНЯМ (выровнен с days,
    каждый день редактируется на фронте). Если задан: каждый CompensationDay
    получает свою длительность, requested_duration = их сумма. Сумма может быть
    МЕНЬШЕ суммы выбранных долгов (частичная отработка) — непокрытый остаток при
    верификации возвращается в открытый долг. Если не задан — сумма долгов делится
    поровну (обычная отработка). Оригинальная сумма долгов → claim_metadata.debt_total.

    retroactive=True — оператор уже отработал в прошедшие дни цикла: заявка
    принимается и СРАЗУ проверяется по факту (verify_otrabotka), статус
    становится approved/partial/declined на месте. source → "retroactive".
    """
    if retroactive:
        source = "retroactive"
    cycle, rule, wdds, days, total = _validate_inputs(
        operator, debt_detail_ids, days, retroactive=retroactive,
    )
    day_durations, requested = _resolve_day_durations(durations, days, total)

    conflicts = get_conflicts(operator, days)
    if conflicts:
        notify.notify_operator(operator, "otrabotka_conflict", {
            "conflicts": conflicts, "days": [str(d) for d in days],
        })
        raise OtrabotkaError(
            "На выбранные дни есть заявки Отпрашивание или Перенос "
            "рабочего дня — отработку принять нельзя.",
            code="conflict", payload={"conflicts": conflicts}, http_status=409,
        )

    comp = Compensation.objects.create(
        operator=operator,
        cycle=cycle,
        type_rule=rule,
        source=source,
        status="pending",
        planned_date=min(days),
        requested_duration=requested,
        comment=comment or "",
        pdf_file=pdf_file,
        screens=screens,
        debts_snapshot=[],
        claim_metadata={"debt_total": str(total)},
        auto_check_result={},
        fixed_by=user,
    )

    apply_otrabotka_effects(comp, wdds, days, day_durations)

    # Ретроактивная: оператор уже отработал — проверяем СРАЗУ по факту.
    if retroactive:
        from .otrabotka_verify import verify_otrabotka
        verify_otrabotka(comp)
        comp.refresh_from_db()

    notify.notify_operator(operator, "otrabotka_created", {
        "compensation_id": comp.id,
        "total_duration": str(total),
        "days": [str(d) for d in days],
        "status": comp.status,
        "retroactive": retroactive,
    })

    return comp
