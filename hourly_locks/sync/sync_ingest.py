"""
WFM -> Python generic ingest (two-way CRUD).

Endpoints (wire in urls.py):
    path("api/sync/upsert/<entity>", UpsertView.as_view())
    path("api/sync/delete/<entity>", DeleteView.as_view())

WFM calls these on every create/update/delete. Matching key = external_id (Django PK);
for compensation/transfer, correlation_id is also supported (for WFM-created records).

Conversions: minutes(int)->timedelta, 'HH:MM'->time, ISO 'YYYY-MM-DD'->date.
"""
from datetime import timedelta, datetime

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status as http_status

from .service_auth import HasServiceToken
from hourly_locks.models import (
    Group, Shift, RequestTypeRule, Operator, Cycle,
    WorkDebt, WorkDebtDetail, Compensation, Transfer, CompensationDebtLink,
)


# --- converters ---
def D(v):   # minutes -> timedelta
    return timedelta(minutes=v) if v is not None else None


def T(v):   # 'HH:MM' -> time
    return datetime.strptime(v, "%H:%M").time() if v else None


def DATE(v):  # ISO -> date
    return datetime.fromisoformat(v[:19]).date() if v else None


def P(v):   # plain
    return v


# entity -> (Model, {field: converter})  | fields not listed are ignored
ENTITIES = {
    "operator-groups": (Group, {
        "name": P, "supervisor_id": P, "is_active": P,
    }),
    "shifts": (Shift, {
        "code": P, "display_name": P, "start_time": T, "end_time": T,
        "crosses_midnight": P, "norm_full": D, "norm_lock_soft_cap": D,
        "norm_lock_warn_at": D, "tolerance_undertime": D, "fetch_hour_padding": P,
        "is_night": P, "requires_special_pipeline": P, "is_active": P,
    }),
    "type-rules": (RequestTypeRule, {
        "category": P, "code": P, "display_name": P, "description": P,
        "requires_date_from": P, "requires_date_to": P, "requires_hour_range": P,
        "requires_duration": P, "requires_related_debts": P, "creates_debt_if_unmet": P,
        "exempts_from_daily_debt": P, "auto_approve_on_create": P, "allows_past_date": P,
        "requires_supervisor_approval": P, "forbidden_on_day_off": P,
        "min_duration": D, "max_duration": D, "verification_strategy": P,
        "sort_order": P, "is_active": P,
    }),
    "cycles": (Cycle, {
        "year": P, "month": P, "start_date": DATE, "end_date": DATE,
        "status": P, "archive_stats": P,
    }),
    "operators": (Operator, {
        "login_id": P, "surname": P, "name": P, "middle_name": P,
        "group_id": P, "is_active": P,
    }),
    "work-debts": (WorkDebt, {
        "operator_id": P, "cycle_id": P, "current_debt": D, "total_accumulated": D,
    }),
    "work-debt-details": (WorkDebtDetail, {
        "operator_id": P, "day": DATE, "cycle_id": P, "source": P, "source_object_id": P,
        "shift_id": P, "shift_code_snapshot": P, "violation_type": P,
        "norm_full": D, "fact_full": D, "debt_full": D, "norm_lock": D, "fact_lock": D,
        "debt_lock": D, "make_up_wh": D, "locked_for_compensation": P, "note": P,
    }),
    "compensations": (Compensation, {
        "operator_id": P, "cycle_id": P, "status": P, "comment": P, "type_rule_id": P,
        "source": P, "planned_date": DATE, "requested_duration": D, "verified_duration": D,
        "remaining_debt": D, "deducted": P, "correlation_id": P,
    }),
    "transfers": (Transfer, {
        "operator_id": P, "cycle_id": P, "status": P, "comment": P, "type_rule_id": P,
        "date_from": DATE, "date_to": DATE, "hour_from": T, "hour_to": T,
        "requested_duration": D, "verified_duration": D, "remaining_debt": D, "correlation_id": P,
    }),
    "compensation-debt-links": (CompensationDebtLink, {
        "compensation_id": P, "debt_detail_id": P, "snapshot": P, "applied": P, "applied_amount": D,
    }),
}


def _build_defaults(spec, data):
    out = {}
    for field, conv in spec.items():
        if field in data:
            out[field] = conv(data[field])
    return out


import logging as _logging
_log = _logging.getLogger("wfm_sync")

# WFM -> Django bu 5 entity'dan biriga POST (upsert/delete) kelsa, o'sha operatorning
# TO'LIQ holati (operator + umumiy qarz + qarz detali + compensation + transfer)
# WFM'ga darhol (sinxron) qayta push qilinadi (A1).
_TRIGGER_ENTITIES = {
    "operators", "work-debts", "work-debt-details", "compensations", "transfers",
}


def _operator_id_of(entity, obj):
    if obj is None:
        return None
    if entity == "operators":
        return obj.id
    return getattr(obj, "operator_id", None)


def _sync_operator_state(entity, operator_id):
    """Ta'sirlangan operatorning to'liq holatini WFM'ga sinxron push. Best-effort —
    xato bo'lsa asosiy upsert/delete javobini buzmaydi."""
    if entity not in _TRIGGER_ENTITIES or not operator_id:
        return
    try:
        from hourly_locks.services.cycle import get_active_cycle
        from hourly_locks.sync.runners_sync import push_after_apply
        push_after_apply([operator_id], get_active_cycle())
    except Exception as exc:
        _log.warning("ingest sync_operator_state(%s, op=%s) xato: %s", entity, operator_id, exc)


class UpsertView(APIView):
    permission_classes = [HasServiceToken]

    def post(self, request, entity):
        cfg = ENTITIES.get(entity)
        if not cfg:
            return Response({"ok": False, "message": f"unknown entity: {entity}"},
                            status=http_status.HTTP_400_BAD_REQUEST)
        Model, spec = cfg
        data = request.data
        defaults = _build_defaults(spec, data)
        ext = data.get("external_id")
        corr = data.get("correlation_id")

        try:
            if ext:
                obj, _ = Model.objects.update_or_create(id=ext, defaults=defaults)
            elif corr and "correlation_id" in spec:
                obj, _ = Model.objects.update_or_create(correlation_id=corr, defaults=defaults)
            else:
                obj = Model.objects.create(**defaults)
        except Exception as exc:
            return Response({"ok": False, "message": str(exc)},
                            status=http_status.HTTP_400_BAD_REQUEST)

        # A1: shu operatorning to'liq holatini WFM'ga sinxron push.
        _sync_operator_state(entity, _operator_id_of(entity, obj))

        return Response({"ok": True, "external_id": obj.id, "correlation_id": corr})


class DeleteView(APIView):
    permission_classes = [HasServiceToken]

    def post(self, request, entity):
        cfg = ENTITIES.get(entity)
        if not cfg:
            return Response({"ok": False, "message": f"unknown entity: {entity}"},
                            status=http_status.HTTP_400_BAD_REQUEST)
        Model, spec = cfg
        data = request.data
        ext = data.get("external_id")
        corr = data.get("correlation_id")

        qs = None
        if ext:
            qs = Model.objects.filter(id=ext)
        elif corr and "correlation_id" in spec:
            qs = Model.objects.filter(correlation_id=corr)

        # operator_id ni o'chirishdan OLDIN olamiz (keyin obj bo'lmaydi).
        op_id = _operator_id_of(entity, qs.first()) if qs is not None else None

        deleted = 0
        if qs is not None:
            deleted, _ = qs.delete()

        # A1: o'zgargan operator holatini WFM'ga sinxron push.
        _sync_operator_state(entity, op_id)

        return Response({"ok": True, "deleted": deleted})
