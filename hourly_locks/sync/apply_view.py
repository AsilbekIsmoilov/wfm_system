"""
Oqim A — inbound endpoint that WFM calls synchronously when a compensation/transfer
is submitted on the platform.

Uses your real models/services. correlation_id provides idempotency: if WFM retries
the same request, the already-created record is returned instead of creating a duplicate.

WFM call:  POST {PYTHON}/api/sync/apply-request   (header: x-sync-token)
Wire it in urls.py:
    path("api/sync/apply-request", ApplyRequestView.as_view())
"""
from datetime import timedelta

from django.db import transaction
from django.utils.dateparse import parse_date
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status as http_status

from .service_auth import HasServiceToken

from hourly_locks.models import Operator, RequestTypeRule, Compensation, Transfer, WorkDebt
from hourly_locks.services.cycle import get_or_create_active_cycle
from hourly_locks.services.compensation_verifier import verify_single_compensation


def _min_to_td(minutes):
    return timedelta(minutes=minutes) if minutes is not None else None


def _td_to_min(td):
    return int(td.total_seconds() // 60) if td else 0


def _work_debt_payload(operator, cycle):
    wd = WorkDebt.objects.filter(operator=operator, cycle=cycle).first()
    if not wd:
        return None
    return {
        "external_id": wd.id,
        "operator_id": operator.id,
        "cycle_id": cycle.id,
        "current_debt": _td_to_min(wd.current_debt),
        "total_accumulated": _td_to_min(wd.total_accumulated),
    }


class ApplyRequestView(APIView):
    permission_classes = [HasServiceToken]

    def post(self, request):
        data = request.data
        category = data.get("category") 
        correlation_id = data.get("correlation_id")
        operator_ext = data.get("operator_external_id")
        type_rule_code = data.get("type_rule_code")

        # 1) Resolve references
        try:
            operator = Operator.objects.get(id=operator_ext)
            rule = RequestTypeRule.objects.get(
                code=type_rule_code, category=category, is_active=True,
            )
        except (Operator.DoesNotExist, RequestTypeRule.DoesNotExist):
            return Response(
                {"ok": False, "message": "operator or type_rule not found"},
                status=http_status.HTTP_404_NOT_FOUND,
            )

        cycle = get_or_create_active_cycle()
        Model = Compensation if category == "compensation" else Transfer

        # 2) Idempotency: if this correlation_id was already applied, return it
        if correlation_id:
            existing = Model.objects.filter(correlation_id=correlation_id).first()
            if existing:
                return Response(self._response(existing, category, operator, cycle))

        try:
            with transaction.atomic():
                if category == "compensation":
                    return self._apply_compensation(data, operator, rule, cycle, correlation_id)
                else:
                    return self._create_transfer(data, operator, rule, cycle, correlation_id)
        except Exception as exc:
            return Response(
                {"ok": False, "message": str(exc)},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

    def _apply_compensation(self, data, operator, rule, cycle, correlation_id):
        related_debts = data.get("related_debts") or []
        auto_sum = data.get("auto_sum", True)

        comp = Compensation.objects.create(
            operator=operator,
            cycle=cycle,
            type_rule=rule,
            source=data.get("source", "requested"),
            planned_date=parse_date(data["planned_date"][:10]) if data.get("planned_date") else None,
            requested_duration=_min_to_td(data.get("requested_duration")),
            comment=data.get("comment", ""),
            status="pending",
            correlation_id=correlation_id,
        )


        if related_debts:
            from hourly_locks.services.compensation_verifier import link_selected_debts
            total = link_selected_debts(comp, related_debts, cycle)
            if auto_sum and total > timedelta(0):
                comp.requested_duration = total
                comp.save(update_fields=["requested_duration", "updated_at"])

        # Immediate types (exception, sl, wc, study, vacation, no_compensation) -> verify now.
        # Deferred types (compensation, nb_compensation, partial_exception) -> runner handles.
        if rule.auto_approve_on_create:
            verify_single_compensation(comp, comp.planned_date, cycle)
            comp.refresh_from_db()

        return Response(self._response(comp, "compensation", operator, cycle))

    # --- Transfer: just create (runners process it); no immediate debt change today ---
    def _create_transfer(self, data, operator, rule, cycle, correlation_id):
        tr = Transfer.objects.create(
            operator=operator,
            cycle=cycle,
            type_rule=rule,
            date_from=parse_date(data["date_from"][:10]) if data.get("date_from") else None,
            date_to=parse_date(data["date_to"][:10]) if data.get("date_to") else None,
            hour_from=data.get("hour_from") or None,
            hour_to=data.get("hour_to") or None,
            requested_duration=_min_to_td(data.get("requested_duration")),
            comment=data.get("comment", ""),
            status="pending",
            correlation_id=correlation_id,
        )
        return Response(self._response(tr, "transfer", operator, cycle))

    # --- Shared response builder ---
    def _response(self, obj, category, operator, cycle):
        out = {
            "ok": True,
            "request_external_id": obj.id,
            "status": obj.status,
            "verified_duration": _td_to_min(getattr(obj, "verified_duration", None)),
            "remaining_debt": _td_to_min(getattr(obj, "remaining_debt", None)),
            "work_debt": None,
        }
        # Only compensations change debt synchronously
        if category == "compensation":
            out["work_debt"] = _work_debt_payload(operator, cycle)
        else:
            out["message"] = "transfer created; processed by runner"
        return out
