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
from hourly_locks.utils import format_duration


def _td_to_min(td):
    return int(td.total_seconds() // 60) if td else 0


def _created_by(raw):
    """
    WFM konvertidagi `created_by` — {"id": "<mongo ObjectId>", "full_name": "..."}.
    Django-user emas, shuning uchun FK emas, oddiy snapshot sifatida saqlanadi.
    Yo'q/buzuq bo'lsa None — ariza baribir yaratiladi (audit maydoni majburiy emas).
    """
    if not isinstance(raw, dict):
        return None
    author_id = raw.get("id")
    if not author_id:
        return None
    return {
        "id": str(author_id)[:64],
        "name": str(raw.get("full_name") or "")[:255] or None,
    }


def _work_debt_payload(operator, cycle):
    wd = WorkDebt.objects.filter(operator=operator, cycle=cycle).first()
    if not wd:
        return None
    return {
        "external_id": wd.id,
        "operator_id": operator.id,
        "cycle_id": cycle.id,
        # HH:MM:SS (WFM parses to seconds); other duration fields stay minutes.
        "current_debt": format_duration(wd.current_debt),
        "total_accumulated": format_duration(wd.total_accumulated),
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
        # WFM foydalanuvchisi (Django user emas) — konvertda keladi, natijaga
        # _comp_result/_transfer_result ichida shtamplanadi.
        self._created_by = _created_by(data.get("created_by"))

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

        # A1: ariza qabul qilingach, ta'sirlangan operator(lar) holatini WFM'ga
        # DARHOL push (javob qaytishdan oldin). Xato bo'lsa arizani buzmaydi —
        # yozuv Django'da bor, keyingi kunlik push baribir sinxronlaydi.
        try:
            from hourly_locks.services.cycle import get_active_cycle
            from hourly_locks.sync.runners_sync import push_after_apply
            op_ids = set()
            if result.get("compensation_id"):
                c = Compensation.objects.filter(id=result["compensation_id"]).first()
                if c:
                    op_ids.add(c.operator_id)
            for tid in (result.get("transfer_ids") or []):
                t = Transfer.objects.filter(id=tid).first()
                if t:
                    op_ids.add(t.operator_id)
            if op_ids:
                push_after_apply(op_ids, get_active_cycle())
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "apply-request WFM push_after_apply xato: %s", exc)

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
        fields = []
        if correlation_id and not comp.correlation_id:
            comp.correlation_id = correlation_id
            fields.append("correlation_id")
        author = getattr(self, "_created_by", None)
        if author:
            comp.wfm_created_by_id = author["id"]
            comp.wfm_created_by_name = author["name"]
            fields += ["wfm_created_by_id", "wfm_created_by_name"]
        if fields:
            comp.save(update_fields=fields)
        return {
            "type": rtype,
            "compensation_id": comp.id,
            "status": comp.status,
            "work_debt": _work_debt_payload(comp.operator, comp.cycle),
        }

    def _transfer_result(self, result, rtype, correlation_id):
        ids = [c.get("transfer_id") for c in (result.get("created") or [])
               if c.get("transfer_id")]
        if ids:
            updates = {}
            if correlation_id:
                updates["correlation_id"] = correlation_id
            author = getattr(self, "_created_by", None)
            if author:
                updates["wfm_created_by_id"] = author["id"]
                updates["wfm_created_by_name"] = author["name"]
            if updates:
                Transfer.objects.filter(id__in=ids).update(**updates)
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
            durations=data.get("durations"),
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
            operators=ops, leave_days=data["days"], hour_from=data["hour_from"],
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
            hour_to=data.get("hour_to"), duration=data.get("duration"),
            comment=data.get("comment", ""),
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


class EditCompensationView(APIView):
    """WFM PATCH /compensations/:id → shu endpoint.

    Django edit_compensation'ni chaqiradi (revert + re-apply BIR XIL record'da,
    PK barqaror → WFM sync churn yo'q). Tur bo'yicha:
      - otrabotka          — faqat pending: days va/yoki debt_detail_ids
      - qolgan (oправдание) — approved: debt_detail_ids va/yoki applied_duration
    So'ng A1: ta'sirlangan operator holatini WFM'ga darhol push qiladi.

    Body: {
      "compensation_id": <django pk = external_id>,   # majburiy
      "days": ["YYYY-MM-DD", ...],            # ixtiyoriy (otrabotka)
      "debt_detail_ids": [<wdd id>, ...],     # ixtiyoriy
      "applied_duration": "H:MM",             # ixtiyoriy (oправдание)
      "comment": "..."                        # ixtiyoriy
    }
    Berilmagan maydonlar joriy arizadan olinadi.
    """
    permission_classes = [HasServiceToken]

    def patch(self, request):
        return self._handle(request)

    def post(self, request):
        return self._handle(request)

    def _handle(self, request):
        from hourly_locks.services.compensation_edit import (
            edit_compensation, CompensationEditError,
        )
        data = request.data
        comp_id = data.get("compensation_id") or data.get("external_id")
        if not comp_id:
            return Response(
                {"ok": False, "code": "no_id",
                 "detail": "compensation_id majburiy"},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        comp = Compensation.objects.filter(id=comp_id).first()
        if not comp:
            return Response(
                {"ok": False, "code": "not_found",
                 "detail": f"compensation topilmadi: {comp_id}"},
                status=http_status.HTTP_404_NOT_FOUND,
            )

        # Faqat berilgan (None emas) maydonlarni uzatamiz — qolgani joriy holatdan.
        # durations — otrabotka (per-day), applied_duration — debt-justification.
        kwargs = {}
        for key in ("days", "debt_detail_ids", "durations", "applied_duration"):
            if data.get(key) is not None:
                kwargs[key] = data[key]
        if "comment" in data:
            kwargs["comment"] = data["comment"]

        try:
            with transaction.atomic():
                comp = edit_compensation(comp, **kwargs)
        except CompensationEditError as exc:
            return Response(
                {"ok": False, "code": exc.code, "detail": exc.message, **exc.payload},
                status=exc.http_status,
            )
        except Exception as exc:
            return Response(
                {"ok": False, "code": "error", "detail": str(exc)},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        # A1: operator holatini WFM'ga darhol sinxronla (xato bo'lsa ariza buzilmaydi).
        try:
            from hourly_locks.services.cycle import get_active_cycle
            from hourly_locks.sync.runners_sync import push_after_apply
            push_after_apply({comp.operator_id}, get_active_cycle())
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "edit-compensation WFM push xato: %s", exc)

        return Response({
            "ok": True,
            "type": "compensation_edit",
            "compensation_id": comp.id,
            "status": comp.status,
            "work_debt": _work_debt_payload(comp.operator, comp.cycle),
        })


class EditTransferView(APIView):
    """WFM PATCH /transfers/:id → shu endpoint.

    Django edit_transfer'ni chaqiradi (revert + re-apply BIR XIL Transfer'da,
    PK barqaror). Tur bo'yicha:
      - otprashivanie   — leave_days (days) / workoff_days / hours; otgul VA
        bog'liq otrabotka comp kunlari qayta yaratiladi. Comp verified bo'lsa blok.
      - ucheba_rabochee — days / hours; UchebaDay qayta, status → pending.
      - qolganlari (benefit: льготы, perenos_dnya, ...) — days / hours / subtype.
    completed / declined — blok. So'ng A1: operator holatini WFM'ga push.

    Body: {
      "transfer_id": <django pk = external_id>,   # majburiy
      "days": ["YYYY-MM-DD", ...],           # ixtiyoriy (otgul/льгота/ucheba kunlari)
      "workoff_days": ["YYYY-MM-DD", ...],   # ixtiyoriy (otprashivanie otrabotka kunlari)
      "hour_from": "HH:MM", "hour_to": "HH:MM",  # ixtiyoriy
      "subtype": "otgul|korotkiy_den",       # ixtiyoriy (lgota_prochie)
      "comment": "..."                       # ixtiyoriy
    }
    Berilmagan maydonlar joriy arizadan olinadi.
    """
    permission_classes = [HasServiceToken]

    def patch(self, request):
        return self._handle(request)

    def post(self, request):
        return self._handle(request)

    def _handle(self, request):
        from hourly_locks.services.transfer_edit import (
            edit_transfer, TransferEditError,
        )
        data = request.data
        tr_id = data.get("transfer_id") or data.get("external_id")
        if not tr_id:
            return Response(
                {"ok": False, "code": "no_id", "detail": "transfer_id majburiy"},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        tr = Transfer.objects.filter(id=tr_id).first()
        if not tr:
            return Response(
                {"ok": False, "code": "not_found",
                 "detail": f"transfer topilmadi: {tr_id}"},
                status=http_status.HTTP_404_NOT_FOUND,
            )

        kwargs = {}
        for key in ("days", "workoff_days", "hour_from", "hour_to", "subtype", "duration"):
            if data.get(key) is not None:
                kwargs[key] = data[key]
        if "comment" in data:
            kwargs["comment"] = data["comment"]

        try:
            with transaction.atomic():
                result = edit_transfer(tr, **kwargs)
        except TransferEditError as exc:
            return Response(
                {"ok": False, "code": exc.code, "detail": exc.message, **exc.payload},
                status=exc.http_status,
            )
        except Exception as exc:
            return Response(
                {"ok": False, "code": "error", "detail": str(exc)},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        # A1: operator holatini WFM'ga darhol sinxronla.
        try:
            from hourly_locks.services.cycle import get_active_cycle
            from hourly_locks.sync.runners_sync import push_after_apply
            tr.refresh_from_db()
            push_after_apply({tr.operator_id}, get_active_cycle())
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "edit-transfer WFM push xato: %s", exc)

        return Response({"ok": True, "type": "transfer_edit", **result})


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
