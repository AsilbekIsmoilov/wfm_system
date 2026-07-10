"""
Прямое списание долга компенсацией (Compensation, любой тип КРОМЕ «otrabotka»).

Типы: otprashivanie, isklyuchenie, ne_otrabotaet, ucheba_rabochee,
perenos_dnya, obuchenie, льготы (lgota*), hoz_raboty — все из категории
«compensation», кроме отработки.

Идея (в отличие от отработки — БЕЗ расчёта по факту/сменам):
  - оператор выбирает свои записи долга (WorkDebtDetail);
  - заявка такого типа ОПРАВДЫВАЕТ долг напрямую — при приёме привязанные
    записи долга «переезжают» в заявку (CompensationDebtLink, applied=True,
    WDD блокируется) и их суммарный долг СПИСЫВАЕТСЯ из WorkDebt.current_debt;
  - дополнительно: супервайзер может ВРУЧНУЮ уменьшить итоговую списываемую
    сумму (applied_duration < сумма выбранных долгов). Тогда списывается
    ровно это значение, ВСЕ выбранные записи всё равно «переезжают» в заявку,
    а на остаток (сумма − applied) создаётся ОДНА новая запись долга
    (source="compensation_remainder"), которую оператор должен ещё закрыть.

Математика списания (recompute_for_operator):
  current_debt = Σ(WDD.debt) − Σ(link.applied_amount, applied=True)
  - links.applied_amount = ПОЛНЫЙ долг каждой выбранной WDD (Σ = total);
  - новая запись остатка добавляет (total − applied) обратно в Σ(WDD.debt);
  - итог: current_debt уменьшается ровно на applied (V).
"""

from datetime import date, timedelta

from django.db import transaction
from django.utils.timezone import now

from ..models import (
    Compensation,
    CompensationDebtLink,
    RequestTypeRule,
    WorkDebtDetail,
)
from . import notify
from .cycle import get_or_create_active_cycle
from .otrabotka import OTRABOTKA_CODE
from .work_debt import recompute_for_operator

REMAINDER_SOURCE = "compensation_remainder"


class DebtCompensationError(Exception):
    """Бизнес-ошибка прямого списания долга (мапится в HTTP 4xx)."""

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

def _parse_duration(value) -> timedelta:
    """timedelta | 'H:MM[:SS]' | секунды(int/str) → timedelta."""
    if value is None:
        return None
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(seconds=int(value))
    s = str(value).strip()
    if not s:
        return None
    if ":" in s:
        parts = [int(p) for p in s.split(":")]
        while len(parts) < 3:
            parts.append(0)
        h, m, sec = parts[0], parts[1], parts[2]
        return timedelta(hours=h, minutes=m, seconds=sec)
    return timedelta(seconds=int(s))


def _wdd_total(w) -> timedelta:
    return (w.debt_full or timedelta(0)) + (w.debt_lock or timedelta(0))


def _validate(operator, code, debt_detail_ids, applied_duration):
    """Общие проверки preview/accept. Возвращает (cycle, rule, wdds, total, applied, remainder)."""
    cycle = get_or_create_active_cycle()

    if code == OTRABOTKA_CODE:
        raise DebtCompensationError(
            "Для отработки используйте отдельный приём (accept_otrabotka).",
            code="use_otrabotka",
        )
    try:
        rule = RequestTypeRule.objects.get(
            code=code, category="compensation", is_active=True,
        )
    except RequestTypeRule.DoesNotExist:
        raise DebtCompensationError(
            f"Тип «{code}» (compensation) не найден или неактивен.",
            code="type_missing", http_status=404,
        )

    if not debt_detail_ids:
        raise DebtCompensationError("Не выбрана ни одна запись долга.", code="no_debts")

    wdds = list(
        WorkDebtDetail.objects.filter(id__in=debt_detail_ids, operator=operator)
    )
    found = {w.id for w in wdds}
    missing = [i for i in debt_detail_ids if i not in found]
    if missing:
        raise DebtCompensationError(
            f"Записи долга не найдены у оператора: {missing}.",
            code="debts_not_found", payload={"missing": missing},
        )

    locked = [w.id for w in wdds if w.locked_for_compensation]
    if locked:
        raise DebtCompensationError(
            f"Записи долга уже привязаны к другой заявке: {locked}.",
            code="debts_locked", payload={"locked": locked},
        )

    total = timedelta(0)
    for w in wdds:
        total += _wdd_total(w)
    if total <= timedelta(0):
        raise DebtCompensationError(
            "Сумма выбранных долгов равна нулю.", code="zero_debt",
        )

    applied = _parse_duration(applied_duration)
    if applied is None:
        applied = total
    if applied <= timedelta(0):
        raise DebtCompensationError(
            "Списываемая сумма должна быть больше нуля.", code="bad_applied",
        )
    if applied > total:
        raise DebtCompensationError(
            f"Списываемая сумма ({applied}) больше суммы выбранных долгов ({total}).",
            code="applied_too_big",
            payload={"applied": str(applied), "total": str(total)},
        )

    remainder = max(total - applied, timedelta(0))
    return cycle, rule, wdds, total, applied, remainder


def _split_proportional(remainder, total_full, total_lock):
    """Разбить остаток на (full, lock) пропорционально исходному соотношению."""
    base = total_full + total_lock
    if base <= timedelta(0) or remainder <= timedelta(0):
        return remainder, timedelta(0)
    rem_sec = int(remainder.total_seconds())
    full_sec = rem_sec * int(total_full.total_seconds()) // int(base.total_seconds())
    return timedelta(seconds=full_sec), timedelta(seconds=rem_sec - full_sec)


# --------------------------------------------------------------------------- #
# Preview / Accept
# --------------------------------------------------------------------------- #

def preview_debt_compensation(*, operator, code, debt_detail_ids,
                              applied_duration=None) -> dict:
    """Сухой прогон: сумма долгов, списываемая сумма, остаток. Без записи в БД."""
    cycle, rule, wdds, total, applied, remainder = _validate(
        operator, code, debt_detail_ids, applied_duration,
    )
    return {
        "ok": True,
        "operator_id": operator.id,
        "type": {"code": rule.code, "display_name": rule.display_name},
        "selected_debts": [
            {"id": w.id, "day": str(w.day), "debt": str(_wdd_total(w))}
            for w in wdds
        ],
        "total_duration": str(total),
        "applied_duration": str(applied),
        "remainder": str(remainder),
        "confirm_prompt": (
            f"Списать {applied} долга по типу «{rule.display_name}»"
            + (f" (остаток {remainder} останется долгом)" if remainder > timedelta(0) else "")
            + "?"
        ),
    }


@transaction.atomic
def accept_debt_compensation(*, operator, code, debt_detail_ids,
                             applied_duration=None, comment="", user=None,
                             source="requested", pdf_file=None,
                             screens=None) -> Compensation:
    """
    Принять заявку-оправдание: списать долг, «переселить» записи долга в заявку,
    при уменьшении суммы — создать одну запись на остаток. Возвращает Compensation.
    """
    cycle, rule, wdds, total, applied, remainder = _validate(
        operator, code, debt_detail_ids, applied_duration,
    )

    comp = Compensation.objects.create(
        operator=operator,
        cycle=cycle,
        type_rule=rule,
        source=source,
        status="approved",
        planned_date=date.today(),
        requested_duration=total,
        verified_duration=applied,
        remaining_debt=remainder,
        deducted=True,
        comment=comment or "",
        pdf_file=pdf_file,
        screens=screens,
        debts_snapshot=[],
        claim_metadata={},
        auto_check_result={},
        fixed_by=user,
        verified_at=now(),
    )

    apply_debt_effects(comp, wdds, rule, total, applied, remainder)

    recompute_for_operator(operator, cycle)

    notify.notify_operator(operator, "debt_compensation_created", {
        "compensation_id": comp.id,
        "type": rule.code,
        "total_duration": str(total),
        "applied_duration": str(applied),
        "remainder": str(remainder),
        "status": comp.status,
    })

    return comp


def apply_debt_effects(comp, wdds, rule, total, applied, remainder):
    """«Переезд» выбранных WDD в заявку-оправдание: applied-связки на полный долг,
    блокировка WDD и (при частичном списании) одна запись-остаток. Единственный
    источник эффектов оправдания — используется при приёме (accept) и при
    редактировании (compensation_edit). `comp` уже создан/обновлён с полями
    requested_duration / verified_duration / remaining_debt."""
    operator = comp.operator
    cycle = comp.cycle
    snapshots = []
    total_full = timedelta(0)
    total_lock = timedelta(0)
    for w in wdds:
        amount = _wdd_total(w)
        total_full += (w.debt_full or timedelta(0))
        total_lock += (w.debt_lock or timedelta(0))
        snap = {
            "id": w.id, "day": str(w.day), "source": w.source,
            "shift_code": w.shift_code_snapshot,
            "debt_full": str(w.debt_full), "debt_lock": str(w.debt_lock),
            "note": w.note or "",
        }
        CompensationDebtLink.objects.create(
            compensation=comp, debt_detail=w, snapshot=snap,
            applied=True, applied_amount=amount,
        )
        snapshots.append(snap)
        w.locked_for_compensation = True
        w.note = (w.note or "") + (
            f"\nПеренесён в заявку «{rule.display_name}» #{comp.id} "
            f"({now():%Y-%m-%d %H:%M})"
        )
        w.save(update_fields=["locked_for_compensation", "note", "updated_at"])

    # Остаток (если сумму уменьшили вручную) → одна новая запись долга.
    if remainder > timedelta(0):
        rem_full, rem_lock = _split_proportional(remainder, total_full, total_lock)
        if rem_full > timedelta(0) and rem_lock > timedelta(0):
            vtype = "both_violations"
        elif rem_lock > timedelta(0):
            vtype = "exceeding_break"
        else:
            vtype = "insufficient_wh"
        WorkDebtDetail.objects.create(
            operator=operator,
            day=max(w.day for w in wdds),
            cycle=cycle,
            source=REMAINDER_SOURCE,
            source_object_id=comp.id,
            violation_type=vtype,
            debt_full=rem_full,
            debt_lock=rem_lock,
            locked_for_compensation=False,
            note=(
                f"Остаток долга {remainder} после частичного списания "
                f"заявкой «{rule.display_name}» #{comp.id} "
                f"(списано {applied} из {total})."
            ),
        )

    comp.debts_snapshot = snapshots
    comp.save(update_fields=["debts_snapshot", "updated_at"])


def revert_debt_effects(comp):
    """Откатить эффекты оправдания: удалить запись-остаток этой заявки,
    разблокировать и полностью отвязать WDD (удалить CompensationDebtLink).
    Статус НЕ трогаем — им управляет вызывающий код. Идемпотентно.
    В отличие от cancel_debt_compensation (которая ставит declined и лишь
    сбрасывает applied), здесь связки удаляются — для чистого повторного
    применения при редактировании."""
    WorkDebtDetail.objects.filter(
        source=REMAINDER_SOURCE, source_object_id=comp.id,
    ).delete()
    for link in CompensationDebtLink.objects.filter(
        compensation=comp,
    ).select_related("debt_detail"):
        w = link.debt_detail
        if w and w.locked_for_compensation:
            w.locked_for_compensation = False
            w.save(update_fields=["locked_for_compensation", "updated_at"])
        link.delete()
    if comp.debts_snapshot:
        comp.debts_snapshot = []
        comp.save(update_fields=["debts_snapshot", "updated_at"])


@transaction.atomic
def cancel_debt_compensation(compensation) -> dict:
    """
    Отменить прямое списание: вернуть записи долга в открытый список,
    удалить запись остатка, пересчитать долг. Идемпотентно.
    """
    if (not compensation.type_rule
            or compensation.type_rule.code == OTRABOTKA_CODE):
        return {"error": "not_debt_compensation"}

    # удалить запись остатка, созданную этой заявкой
    WorkDebtDetail.objects.filter(
        source=REMAINDER_SOURCE, source_object_id=compensation.id,
    ).delete()

    # вернуть привязанные WDD в открытый список
    for link in CompensationDebtLink.objects.filter(
        compensation=compensation,
    ).select_related("debt_detail"):
        if link.debt_detail:
            link.debt_detail.locked_for_compensation = False
            link.debt_detail.save(
                update_fields=["locked_for_compensation", "updated_at"]
            )
        link.applied = False
        link.applied_amount = None
        link.save(update_fields=["applied", "applied_amount", "updated_at"])

    compensation.status = "declined"
    compensation.deducted = False
    compensation.save(update_fields=["status", "deducted", "updated_at"])

    recompute_for_operator(compensation.operator, compensation.cycle)
    return {"compensation_id": compensation.id, "status": "declined"}
