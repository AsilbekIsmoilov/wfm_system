"""
Редактирование ПРИНЯТОЙ заявки-компенсации без смены PK (стабильный external_id
для синхронизации с WFM). Модель «revert + re-apply на том же comp»:

  • otrabotka — только в статусе pending: можно менять days и/или набор WDD
    (debt_detail_ids). Сумма и распределение по дням пересчитываются
    автоматически (split_duration), как при приёме. work_debt пока не трогается
    (пересчёт долга — на этапе верификации).

  • остальные типы (оправдание долга, auto-approve) — можно менять набор WDD
    и/или applied_duration; work_debt и work_debt_details пересчитываются сразу
    (recompute_for_operator).

Дочерние записи (CompensationDay / CompensationDebtLink / запись-остаток)
полностью пересоздаются на каждом редактировании, поэтому результат всегда
консистентен и идемпотентен. Пропущенные поля берутся из текущей заявки.
"""
from datetime import date as _date, timedelta

from django.db import transaction

from ..models import Compensation
from .otrabotka import (
    OTRABOTKA_CODE,
    _validate_inputs as _validate_otrabotka_inputs,
    _resolve_day_durations,
    apply_otrabotka_effects,
    revert_otrabotka_effects,
    get_conflicts,
    OtrabotkaError,
)
from .debt_compensation import (
    _validate as _validate_debt,
    _wdd_total,
    apply_debt_effects,
    revert_debt_effects,
    DebtCompensationError,
)
from ..models import WorkDebtDetail
from .work_debt import recompute_for_operator


class CompensationEditError(Exception):
    def __init__(self, message, code="error", payload=None, http_status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.payload = payload or {}
        self.http_status = http_status


def _is_otrabotka(comp) -> bool:
    return bool(comp.type_rule) and comp.type_rule.code == OTRABOTKA_CODE


def _current_debt_detail_ids(comp):
    return list(comp.debt_links.values_list("debt_detail_id", flat=True))


def _parse_day(value):
    if isinstance(value, _date):
        return value
    return _date.fromisoformat(str(value))


@transaction.atomic
def edit_compensation(comp, *, days=None, debt_detail_ids=None,
                      durations=None, applied_duration=None,
                      comment=None, user=None):
    """Редактировать принятую компенсацию на том же record. Диспетчеризация по
    типу. Возвращает обновлённый Compensation. Ошибки → CompensationEditError."""
    comp = Compensation.objects.select_for_update().get(pk=comp.pk)

    if _is_otrabotka(comp):
        return _edit_otrabotka(
            comp, days=days, debt_detail_ids=debt_detail_ids,
            durations=durations, comment=comment, user=user,
        )
    # Только отработка редактируется. Остальные компенсации (оправдание долга —
    # авто-одобрение, немедленное списание) НЕ редактируются: правка ломала бы
    # уже применённый зачёт work_debt.
    code = comp.type_rule.code if comp.type_rule else None
    name = comp.type_rule.display_name if comp.type_rule else (code or "?")
    from ..models import RequestTypeRule
    otr_name = RequestTypeRule.objects.filter(
        code=OTRABOTKA_CODE).values_list("display_name", flat=True).first() or "Отработка"
    raise CompensationEditError(
        f"Редактирование доступно только для «{otr_name}». Заявку типа «{name}» "
        "(авто-одобрение) редактировать нельзя.",
        code="not_editable", payload={"type": code})


def _edit_otrabotka(comp, *, days, debt_detail_ids, durations, comment, user):
    if comp.status != "pending":
        raise CompensationEditError(
            f"Заявку отработки можно редактировать только в статусе «pending» "
            f"(текущий: «{comp.status}»).",
            code="not_editable", payload={"status": comp.status},
        )

    operator = comp.operator
    # Текущие per-day длительности (ДО отката) — дефолт, если durations не заданы.
    current = {d: dur for d, dur in comp.days.values_list("day", "allocated_duration")}
    # Пропущенные поля → из текущей заявки.
    if debt_detail_ids is None:
        debt_detail_ids = _current_debt_detail_ids(comp)
    if days is None:
        days = list(current.keys())
    days = [_parse_day(d) for d in days]

    # Сначала откат (разблокируем СВОИ WDD и освобождаем СВОИ дни), потом
    # валидация нового набора — иначе проверки locked/occupied упадут на самих себе.
    revert_otrabotka_effects(comp)

    try:
        cycle, rule, wdds, days, total = _validate_otrabotka_inputs(
            operator, debt_detail_ids, days, retroactive=False,
        )
    except OtrabotkaError as exc:
        raise CompensationEditError(
            exc.message, code=exc.code,
            payload=getattr(exc, "payload", {}),
            http_status=getattr(exc, "http_status", 400),
        )

    conflicts = get_conflicts(operator, days)
    if conflicts:
        raise CompensationEditError(
            "На выбранные дни есть заявки Отпрашивание или Перенос рабочего дня "
            "— редактирование отработки невозможно.",
            code="conflict", payload={"conflicts": conflicts}, http_status=409,
        )

    # durations из запроса; если не заданы — сохраняем текущие per-day длительности
    # (когда все дни совпали), иначе None → авто-раздел суммы долгов.
    if durations is None and all(d in current for d in days):
        durations = [current[d] for d in days]
    try:
        day_durations, requested = _resolve_day_durations(durations, days, total)
    except OtrabotkaError as exc:
        raise CompensationEditError(
            exc.message, code=exc.code,
            payload=getattr(exc, "payload", {}),
            http_status=getattr(exc, "http_status", 400),
        )

    comp.requested_duration = requested
    comp.planned_date = min(days)
    meta = dict(comp.claim_metadata or {})
    meta["debt_total"] = str(total)
    comp.claim_metadata = meta
    if comment is not None:
        comp.comment = comment
    comp.save(update_fields=[
        "requested_duration", "planned_date", "claim_metadata", "comment",
        "updated_at",
    ])

    apply_otrabotka_effects(comp, wdds, days, day_durations)
    comp.refresh_from_db()
    return comp


def _edit_debt(comp, *, debt_detail_ids, applied_duration, comment, user):
    if comp.status != "approved":
        raise CompensationEditError(
            f"Заявку-оправдание можно редактировать только в статусе «approved» "
            f"(текущий: «{comp.status}»).",
            code="not_editable", payload={"status": comp.status},
        )

    operator = comp.operator
    code = comp.type_rule.code if comp.type_rule else None
    # Пропущенные поля → из текущей заявки.
    if debt_detail_ids is None:
        debt_detail_ids = _current_debt_detail_ids(comp)
    if applied_duration is None:
        # Сохранить текущий списанный объём, но не больше новой суммы выбранных
        # долгов (иначе при уменьшении набора WDD получили бы applied_too_big).
        new_total = sum(
            (_wdd_total(w) for w in WorkDebtDetail.objects.filter(
                id__in=debt_detail_ids, operator=operator)),
            timedelta(),
        )
        current = comp.verified_duration or new_total
        applied_duration = min(current, new_total) if new_total > timedelta() else current

    # Откат (удаляем остаток, разблокируем и отвязываем СВОИ WDD), затем валидация
    # нового набора (проверка locked не должна споткнуться о собственные WDD).
    revert_debt_effects(comp)

    try:
        cycle, rule, wdds, total, applied, remainder = _validate_debt(
            operator, code, debt_detail_ids, applied_duration,
        )
    except DebtCompensationError as exc:
        raise CompensationEditError(
            exc.message, code=exc.code,
            payload=getattr(exc, "payload", {}),
            http_status=getattr(exc, "http_status", 400),
        )

    comp.requested_duration = total
    comp.verified_duration = applied
    comp.remaining_debt = remainder
    if comment is not None:
        comp.comment = comment
    comp.save(update_fields=[
        "requested_duration", "verified_duration", "remaining_debt",
        "comment", "updated_at",
    ])

    apply_debt_effects(comp, wdds, rule, total, applied, remainder)
    recompute_for_operator(operator, cycle)
    comp.refresh_from_db()
    return comp


def revert_compensation_for_delete(comp):
    """При УДАЛЕНИИ заявки-компенсации: вернуть (разблокировать) ВСЕ её WDD и
    удалить запись-остаток (для оправдания долга). CompensationDay и
    CompensationDebtLink удалит CASCADE; пересчёт долга — в сигнале post_delete
    (после каскадного удаления applied-связок). Идемпотентно."""
    from ..models import WorkDebtDetail, CompensationDebtLink
    from .debt_compensation import REMAINDER_SOURCE
    for link in CompensationDebtLink.objects.filter(
        compensation=comp,
    ).select_related("debt_detail"):
        w = link.debt_detail
        if w and w.locked_for_compensation:
            w.locked_for_compensation = False
            w.save(update_fields=["locked_for_compensation", "updated_at"])
    # запись-остаток, созданную этой заявкой (только у оправдания)
    WorkDebtDetail.objects.filter(
        source=REMAINDER_SOURCE, source_object_id=comp.id,
    ).delete()
