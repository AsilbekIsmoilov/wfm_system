"""
Редактирование ПРИНЯТОЙ заявки-трансфера без смены PK (стабильный external_id
для WFM). Модель revert + re-apply на том же Transfer (а для отпрашивания — ещё
и на связанном otrabotka-Compensation, PK тоже стабилен).

Диспетчеризация по type_rule.code:
  • otprashivanie   — правим дни отгула / дни отработки / часы; пересоздаются и
    OtprashivanieDay, и CompensationDay связанного плана отработки.
    Разрешено, пока transfer не completed/declined И связанный comp = pending
    (если отработку уже верифицировали — редактировать нельзя).
  • ucheba_rabochee — правим дни/часы; пересоздаётся UchebaDay; статус → pending
    (повторная верификация в пайплайне).
  • остальные (льготы, hoz_raboty, perenos_dnya, isklyuchenie, ...) — правим
    дни/часы/подтип; пересоздаётся BenefitDay; авто-approved.

Guard: completed / declined — редактирование запрещено (терминальные статусы);
а также если дата окончания заявки (date_to) уже прошла (сегодня > date_to) —
период завершён, править нельзя.
Пропущенные поля берутся из текущей заявки.
"""
from datetime import date

from django.db import transaction

from ..models import Transfer
from .benefit import (
    BenefitError,
    _validate_common as _validate_benefit,
    _operator_plan as _benefit_plan,
    apply_benefit_effects,
    revert_benefit_effects,
)
from .ucheba import (
    UCHEBA_CODE,
    UchebaError,
    _validate_common as _validate_ucheba,
    _operator_plan as _ucheba_plan,
    apply_ucheba_effects,
    revert_ucheba_effects,
)
from .otprashivanie import (
    OTPR_CODE,
    OtprashivanieError,
    _validate_common as _validate_otpr,
    _validate_operator as _otpr_validate_operator,
    apply_otprashivanie_effects,
    revert_otprashivanie_effects,
)

BLOCKED_STATUSES = {"completed", "declined"}


class TransferEditError(Exception):
    def __init__(self, message, code="error", payload=None, http_status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.payload = payload or {}
        self.http_status = http_status


def _wrap(exc):
    """Business-ошибку сервиса → TransferEditError (единый формат ответа)."""
    return TransferEditError(
        exc.message, code=exc.code,
        payload=getattr(exc, "payload", {}),
        http_status=getattr(exc, "http_status", 400),
    )


@transaction.atomic
def edit_transfer(transfer, *, days=None, hour_from=None, hour_to=None,
                  workoff_days=None, subtype=None, comment=None, user=None):
    """Редактировать принятый трансфер на том же record. Возвращает dict-результат.
    Ошибки → TransferEditError."""
    transfer = Transfer.objects.select_for_update().get(pk=transfer.pk)
    code = transfer.type_rule.code if transfer.type_rule else None

    if transfer.status in BLOCKED_STATUSES:
        raise TransferEditError(
            f"Заявку в статусе «{transfer.status}» редактировать нельзя.",
            code="not_editable", payload={"status": transfer.status})

    # Дата окончания уже прошла → период завершён, править нельзя.
    if transfer.date_to and date.today() > transfer.date_to:
        raise TransferEditError(
            f"Дата окончания заявки ({transfer.date_to}) уже прошла — "
            "редактирование невозможно.",
            code="period_ended", payload={"date_to": str(transfer.date_to)})

    if code == OTPR_CODE:
        return _edit_otprashivanie(
            transfer, leave_days=days, workoff_days=workoff_days,
            hour_from=hour_from, hour_to=hour_to, comment=comment, user=user)
    if code == UCHEBA_CODE:
        return _edit_ucheba(
            transfer, days=days, hour_from=hour_from, hour_to=hour_to,
            comment=comment)
    return _edit_benefit(
        transfer, days=days, hour_from=hour_from, hour_to=hour_to,
        subtype=subtype, comment=comment)


def _edit_benefit(transfer, *, days, hour_from, hour_to, subtype, comment):
    op = transfer.operator
    code = transfer.type_rule.code
    if days is None:
        days = list(transfer.benefit_days.values_list("day", flat=True))
    if hour_from is None:
        hour_from = transfer.hour_from
    if hour_to is None:
        hour_to = transfer.hour_to
    if subtype is None:
        subtype = transfer.subtype

    # Сначала снять свои BenefitDay, иначе _operator_plan сочтёт их «занятыми»
    # (duplicate) и выбросит собственные дни.
    revert_benefit_effects(transfer)
    try:
        ctx = _validate_benefit(code, subtype, days, hour_from, hour_to)
    except BenefitError as exc:
        raise _wrap(exc)
    valid, dropped = _benefit_plan(op, ctx, exclude_transfer_id=transfer.id)
    if not valid:
        raise TransferEditError(
            "После проверки не осталось допустимых дней.",
            code="no_valid_days", payload={"dropped": dropped}, http_status=409)

    if comment is not None:
        transfer.comment = comment
    apply_benefit_effects(transfer, op, valid, ctx)
    transfer.refresh_from_db()
    return {"transfer_id": transfer.id, "status": transfer.status,
            "dropped": dropped}


def _edit_ucheba(transfer, *, days, hour_from, hour_to, comment):
    op = transfer.operator
    if days is None:
        days = list(transfer.ucheba_days.values_list("day", flat=True))
    if hour_from is None:
        hour_from = transfer.hour_from
    if hour_to is None:
        hour_to = transfer.hour_to

    revert_ucheba_effects(transfer)
    try:
        ctx = _validate_ucheba(days, hour_from, hour_to)
    except UchebaError as exc:
        raise _wrap(exc)
    valid, dropped = _ucheba_plan(op, ctx)
    if not valid:
        raise TransferEditError(
            "После проверки не осталось допустимых дней.",
            code="no_valid_days", payload={"dropped": dropped}, http_status=409)

    if comment is not None:
        transfer.comment = comment
    apply_ucheba_effects(transfer, valid, ctx)
    transfer.refresh_from_db()
    return {"transfer_id": transfer.id, "status": transfer.status,
            "dropped": dropped}


def _edit_otprashivanie(transfer, *, leave_days, workoff_days, hour_from,
                        hour_to, comment, user):
    op = transfer.operator
    comp = transfer.repayment_compensation
    # Guard: связанную отработку уже верифицировали → редактировать нельзя.
    if comp is not None and comp.status != "pending":
        raise TransferEditError(
            f"Связанная отработка уже в статусе «{comp.status}» — "
            f"отпрашивание редактировать нельзя.",
            code="workoff_locked", payload={"compensation_status": comp.status})

    if leave_days is None:
        leave_days = list(transfer.otprashivanie_days.values_list("day", flat=True))
    if workoff_days is None:
        workoff_days = list(comp.days.values_list("day", flat=True)) if comp else []
    if hour_from is None:
        hour_from = transfer.hour_from
    if hour_to is None:
        hour_to = transfer.hour_to

    # Снять свои OtprashivanieDay + CompensationDay плана отработки, иначе
    # проверки duplicate/occupied в _validate_operator споткнутся о самих себя.
    revert_otprashivanie_effects(transfer)
    try:
        ctx = _validate_otpr(leave_days, hour_from, hour_to, workoff_days)
    except OtprashivanieError as exc:
        raise _wrap(exc)
    ok, reason = _otpr_validate_operator(op, ctx)
    if not ok:
        raise TransferEditError(
            "Проверка оператора не пройдена для новых дней.",
            code=(reason or {}).get("code", "operator_check_failed"),
            payload=reason or {}, http_status=409)

    if comment is not None:
        transfer.comment = comment
    transfer, comp = apply_otprashivanie_effects(
        transfer, op, ctx, comment=comment, user=user)
    transfer.refresh_from_db()
    return {"transfer_id": transfer.id, "status": transfer.status,
            "compensation_id": comp.id}
