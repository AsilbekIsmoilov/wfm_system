"""
Oqim A — WFM sinxron delegatsiya (Variant A).

WFM platformada ariza qoldirilganda shu endpointni CHAQIRADI. Django ayni
o'sha ariza-qabul logikasini (accept_* servicelari) ishga tushiradi:
  - maydon validatsiyasi  → mavjud *AcceptSerializer'lar (HTTP endpoint bilan bir xil)
  - biznes qoidalari      → accept_* (sikl+past+tur qoidalari, konflikt, qarz-link)
Natija:
  - to'g'ri  → {ok:true, ...}  (ariza qabul qilinadi, qarz o'zgaradi)
  - xato     → {ok:false, code, detail, ...}  (WFM shu xatoni frontga ko'rsatadi)

correlation_id — idempotentlik: WFM retry qilsa, dublikat yaratilmaydi.

WFM call:  POST {PYTHON}/api/sync/apply-request   (header: x-sync-token)
Body:
  {
    "type": "otrabotka|otrabotka_retroactive|debt|otprashivanie|ucheba|benefit",
    "correlation_id": "<wfm-uuid>",       # ixtiyoriy (idempotentlik uchun)
    "source": "requested",                # ixtiyoriy (default: requested)
    "payload": { ... turga xos maydonlar (HTTP endpoint bilan bir xil) ... }
  }
"""
from datetime import timedelta

from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status as http_status

from .service_auth import HasServiceToken

from hourly_locks.models import Operator, Compensation, Transfer, WorkDebt


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
        # Xato klasslari + accept_* — lazy import (circular importdan saqlanish).
        from hourly_locks.services.otrabotka import OtrabotkaError
        from hourly_locks.services.debt_compensation import DebtCompensationError
        from hourly_locks.services.ucheba import UchebaError
        from hourly_locks.services.benefit import BenefitError
        from hourly_locks.services.otprashivanie import OtprashivanieError
        accept_errors = (
            OtrabotkaError, DebtCompensationError, UchebaError,
            BenefitError, OtprashivanieError,
        )

        data = request.data
        rtype = data.get("type")
        correlation_id = data.get("correlation_id") or None
        source = data.get("source") or "requested"
        payload = data.get("payload") or {}

        if rtype not in self._DISPATCH:
            return Response(
                {"ok": False, "code": "unknown_type",
                 "detail": f"noma'lum type: {rtype!r}. Ruxsat: {list(self._DISPATCH)}"},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        # 1) Idempotentlik: shu correlation_id allaqachon qabul qilinganmi?
        if correlation_id:
            existing = self._find_existing(correlation_id)
            if existing is not None:
                return Response({"ok": True, "idempotent": True, **existing})

        # 2) Delegatsiya: serializer (maydon) + accept_* (biznes)
        try:
            with transaction.atomic():
                result = self._DISPATCH[rtype](self, payload, source, correlation_id)
        except _AsResponse as exc:          # serializer maydon xatosi (tayyor Response)
            return exc.response
        except (_ApplyError, *accept_errors) as exc:   # biznes/resolve xatolari
            return Response(
                {"ok": False, "code": exc.code, "detail": exc.message, **exc.payload},
                status=exc.http_status,
            )
        except Exception as exc:            # kutilmagan xato
            return Response(
                {"ok": False, "code": "error", "detail": str(exc)},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        return Response({"ok": True, **result})

    # ------------------------------------------------------------------ #
    # Yordamchilar
    # ------------------------------------------------------------------ #
    def _find_existing(self, correlation_id):
        comp = Compensation.objects.filter(correlation_id=correlation_id).first()
        if comp:
            return {"type": "compensation", "compensation_id": comp.id,
                    "status": comp.status,
                    "work_debt": _work_debt_payload(comp.operator, comp.cycle)}
        tr = Transfer.objects.filter(correlation_id=correlation_id).first()
        if tr:
            return {"type": "transfer", "transfer_ids": [tr.id], "status": tr.status}
        return None

    def _validate(self, serializer_cls, payload):
        """Maydon validatsiyasi — HTTP endpoint bilan bir xil serializer.
        Xato bo'lsa (ok:false, code:validation) Response qaytaradi, aks holda None+data."""
        ser = serializer_cls(data=payload)
        if not ser.is_valid():
            return None, Response(
                {"ok": False, "code": "validation", "errors": ser.errors},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        return ser.validated_data, None

    def _op(self, operator_id):
        try:
            return Operator.objects.get(id=operator_id)
        except Operator.DoesNotExist:
            raise _ApplyError(f"operator topilmadi: {operator_id}", code="operator_not_found",
                              http_status=http_status.HTTP_404_NOT_FOUND)

    def _ops(self, operator_ids):
        ops = list(Operator.objects.filter(id__in=operator_ids))
        found = {o.id for o in ops}
        missing = [i for i in operator_ids if i not in found]
        if missing:
            raise _ApplyError(f"operatorlar topilmadi: {missing}",
                              code="operator_not_found",
                              http_status=http_status.HTTP_404_NOT_FOUND)
        return ops

    def _comp_result(self, comp, rtype, correlation_id):
        if correlation_id and not comp.correlation_id:
            comp.correlation_id = correlation_id
            comp.save(update_fields=["correlation_id"])
        return {
            "type": rtype,
            "compensation_id": comp.id,
            "status": comp.status,
            "work_debt": _work_debt_payload(comp.operator, comp.cycle),
        }

    def _transfer_result(self, result, rtype, correlation_id):
        ids = [c.get("transfer_id") for c in (result.get("created") or [])
               if c.get("transfer_id")]
        if correlation_id and ids:
            Transfer.objects.filter(id__in=ids).update(correlation_id=correlation_id)
        return {"type": rtype, "transfer_ids": ids, "result": result}

    # ------------------------------------------------------------------ #
    # Turga xos handlerlar (views.py bilan aynan bir xil mapping, user=None)
    # ------------------------------------------------------------------ #
    def _h_otrabotka(self, payload, source, cid, retroactive=False):
        from hourly_locks.serializers import OtrabotkaAcceptSerializer
        from hourly_locks.services.otrabotka import accept_otrabotka
        data, err = self._validate(OtrabotkaAcceptSerializer, payload)
        if err:
            raise _AsResponse(err)
        op = self._op(data["operator_id"])
        comp = accept_otrabotka(
            operator=op, debt_detail_ids=data["debt_detail_ids"], days=data["days"],
            comment=data.get("comment", ""), user=None, source=source,
            pdf_file=data.get("pdf_file"), screens=data.get("screens"),
            retroactive=retroactive,
        )
        return self._comp_result(comp, "otrabotka_retroactive" if retroactive else "otrabotka", cid)

    def _h_otrabotka_retro(self, payload, source, cid):
        return self._h_otrabotka(payload, source, cid, retroactive=True)

    def _h_debt(self, payload, source, cid):
        from hourly_locks.serializers import DebtCompensationAcceptSerializer
        from hourly_locks.services.debt_compensation import accept_debt_compensation
        data, err = self._validate(DebtCompensationAcceptSerializer, payload)
        if err:
            raise _AsResponse(err)
        op = self._op(data["operator_id"])
        comp = accept_debt_compensation(
            operator=op, code=data["code"], debt_detail_ids=data["debt_detail_ids"],
            applied_duration=data.get("applied_duration"), comment=data.get("comment", ""),
            user=None, source=source, pdf_file=data.get("pdf_file"),
            screens=data.get("screens"),
        )
        return self._comp_result(comp, "debt", cid)

    def _h_otprashivanie(self, payload, source, cid):
        from hourly_locks.serializers import OtprashivanieAcceptSerializer
        from hourly_locks.services.otprashivanie import accept_otprashivanie
        data, err = self._validate(OtprashivanieAcceptSerializer, payload)
        if err:
            raise _AsResponse(err)
        ops = self._ops(data["operator_ids"])
        result = accept_otprashivanie(
            operators=ops, leave_days=data["leave_days"], hour_from=data["hour_from"],
            hour_to=data["hour_to"], workoff_days=data["workoff_days"],
            comment=data.get("comment", ""), user=None, source=source,
            pdf_file=data.get("pdf_file"), screens=data.get("screens"),
        )
        return self._transfer_result(result, "otprashivanie", cid)

    def _h_ucheba(self, payload, source, cid):
        from hourly_locks.serializers import UchebaAcceptSerializer
        from hourly_locks.services.ucheba import accept_ucheba
        data, err = self._validate(UchebaAcceptSerializer, payload)
        if err:
            raise _AsResponse(err)
        ops = self._ops(data["operator_ids"])
        result = accept_ucheba(
            operators=ops, days=data["days"], hour_from=data["hour_from"],
            hour_to=data["hour_to"], comment=data.get("comment", ""),
            user=None, source=source, pdf_file=data.get("pdf_file"),
            screens=data.get("screens"),
        )
        return self._transfer_result(result, "ucheba", cid)

    def _h_benefit(self, payload, source, cid):
        from hourly_locks.serializers import BenefitAcceptSerializer
        from hourly_locks.services.benefit import accept_benefit, resolve_operators
        data, err = self._validate(BenefitAcceptSerializer, payload)
        if err:
            raise _AsResponse(err)
        if data.get("select_all_shift"):
            ops = resolve_operators(None, data["select_all_shift"], data["days"])
        else:
            ops = self._ops(data.get("operator_ids") or [])
        result = accept_benefit(
            operators=ops, code=data["code"], subtype=data.get("subtype"),
            days=data["days"], hour_from=data.get("hour_from"),
            hour_to=data.get("hour_to"), comment=data.get("comment", ""),
            user=None, source=source, pdf_file=data.get("pdf_file"),
            screens=data.get("screens"),
        )
        return self._transfer_result(result, "benefit", cid)

    _DISPATCH = {
        "otrabotka": _h_otrabotka,
        "otrabotka_retroactive": _h_otrabotka_retro,
        "debt": _h_debt,
        "otprashivanie": _h_otprashivanie,
        "ucheba": _h_ucheba,
        "benefit": _h_benefit,
    }


class _ApplyError(Exception):
    """Sync-view ichki xatosi (accept_* Error naqshi bilan bir xil shakl)."""
    def __init__(self, message, code="error", payload=None,
                 http_status=http_status.HTTP_400_BAD_REQUEST):
        super().__init__(message)
        self.message = message
        self.code = code
        self.payload = payload or {}
        self.http_status = http_status


class _AsResponse(Exception):
    """Serializer validatsiya xatosini (tayyor Response) transaction ichidan chiqarish."""
    def __init__(self, response):
        self.response = response
