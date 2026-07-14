"""
Runner -> WFM push (Oqim C) for ALL operators-module models (two-way sync).
Call push_cycle_to_wfm() at the END of each runner.

Durations -> minutes, EXCEPT work_debt(current_debt/total_accumulated) and
work_debt_detail(norm/fact/debt/make_up_wh) which go as 'HH:MM:SS' (WFM stores seconds).
Times -> 'HH:MM'; dates -> ISO. Upsert key on WFM side = external_id.
"""
import logging

from django.utils import timezone
from hourly_locks.models import (
    Group, Shift, RequestTypeRule, Operator, Cycle,
    WorkDebt, WorkDebtDetail, Compensation, Transfer, CompensationDebtLink,
)

# Watermark stored in SystemPolicy (key/value JSON) so pushes are incremental.
WATERMARK_KEY = "wfm_sync_watermark"


def _load_watermark():
    from hourly_locks.models import SystemPolicy
    row = SystemPolicy.objects.filter(key=WATERMARK_KEY).first()
    if row and isinstance(row.value, dict):
        ts = row.value.get("since")
        if ts:
            from datetime import datetime
            try:
                return datetime.fromisoformat(ts)
            except ValueError:
                return None
    return None


def _save_watermark(dt):
    from hourly_locks.models import SystemPolicy
    SystemPolicy.objects.update_or_create(
        key=WATERMARK_KEY, defaults={"value": {"since": dt.isoformat()}}
    )


def _since_filter(qs, since):
    return qs.filter(updated_at__gt=since) if since else qs
from . import wfm_client
from ..utils import format_duration

logger = logging.getLogger("wfm_sync")
CHUNK = 500


def _min(td):
    return int(td.total_seconds() // 60) if td else 0


def _hhmm(t):
    return t.strftime("%H:%M") if t else None


def _iso(d):
    return d.isoformat() if d else None


def _push(entity, items):
    """Push a list to WFM in chunks via the generic ingest endpoint."""
    if not items:
        return 0
    for i in range(0, len(items), CHUNK):
        wfm_client.push(entity, items[i:i + CHUNK])
    return len(items)


def _reconcile(entity, qs, cycle_id=None, compensation_ids=None):
    """
    Delete-sync: WFM'ga bu scope uchun Django'dagi JORIY external_id ro'yxatini
    yuboradi; WFM ro'yxatda yo'q yozuvlarni prune qiladi (churn-orphan + haqiqiy
    delete + arxiv). To'liq ro'yxat yuborilishi shart — shuning uchun watermark'ga
    bog'liq emas. Xato bo'lsa pipeline'ni buzmaydi (log + davom).
    """
    body = {"external_ids": list(qs.values_list("id", flat=True))}
    if cycle_id is not None:
        body["cycle_id"] = cycle_id
    if compensation_ids is not None:
        body["compensation_ids"] = list(compensation_ids)
    try:
        return wfm_client.reconcile(entity, body)
    except Exception as exc:  # WFM o'chgan bo'lsa ham runner davom etsin
        logger.warning("WFM reconcile(%s) xato: %s", entity, exc)
        return None


def _ser_group(g):
    return {"external_id": g.id, "name": g.name, "supervisor_id": g.supervisor_id, "is_active": g.is_active}


def _ser_shift(s):
    return {
        "external_id": s.id, "code": s.code, "display_name": s.display_name,
        "start_time": _hhmm(s.start_time), "end_time": _hhmm(s.end_time),
        "crosses_midnight": s.crosses_midnight,
        "norm_full": _min(s.norm_full), "norm_lock_soft_cap": _min(s.norm_lock_soft_cap),
        "norm_lock_warn_at": _min(s.norm_lock_warn_at), "tolerance_undertime": _min(s.tolerance_undertime),
        "fetch_hour_padding": s.fetch_hour_padding, "is_night": s.is_night,
        "requires_special_pipeline": s.requires_special_pipeline, "is_active": s.is_active,
    }


def _ser_type_rule(r):
    return {
        "external_id": r.id, "category": r.category, "code": r.code, "display_name": r.display_name,
        "description": r.description, "requires_date_from": r.requires_date_from,
        "requires_date_to": r.requires_date_to, "requires_hour_range": r.requires_hour_range,
        "requires_duration": r.requires_duration, "requires_related_debts": r.requires_related_debts,
        "creates_debt_if_unmet": r.creates_debt_if_unmet, "exempts_from_daily_debt": r.exempts_from_daily_debt,
        "auto_approve_on_create": r.auto_approve_on_create, "allows_past_date": r.allows_past_date,
        "requires_supervisor_approval": r.requires_supervisor_approval,
        "forbidden_on_day_off": r.forbidden_on_day_off,
        "min_duration": _min(r.min_duration) if r.min_duration else None,
        "max_duration": _min(r.max_duration) if r.max_duration else None,
        "verification_strategy": r.verification_strategy, "sort_order": r.sort_order, "is_active": r.is_active,
    }


def _ser_operator(o):
    return {
        "external_id": o.id, "login_id": o.login_id, "surname": o.surname, "name": o.name,
        "middle_name": o.middle_name, "full_name": o.full_name,
        "group_id": o.group_id, "is_active": o.is_active,
    }


def _ser_cycle(c):
    return {
        "external_id": c.id, "year": c.year, "month": c.month,
        "start_date": _iso(c.start_date), "end_date": _iso(c.end_date),
        "status": c.status, "archive_stats": c.archive_stats or {},
    }


def _ser_work_debt(w):
    return {
        "external_id": w.id, "operator_id": w.operator_id, "cycle_id": w.cycle_id,
        # HH:MM:SS (WFM parses to seconds); other duration fields stay minutes.
        "current_debt": format_duration(w.current_debt),
        "total_accumulated": format_duration(w.total_accumulated),
    }


def _ser_detail(d):
    return {
        "external_id": d.id, "operator_id": d.operator_id, "cycle_id": d.cycle_id,
        "day": _iso(d.day), "source": d.source, "source_object_id": d.source_object_id,
        "shift_id": d.shift_id, "shift_code_snapshot": d.shift_code_snapshot, "violation_type": d.violation_type,
        # HH:MM:SS (WFM parses to seconds); other duration fields stay minutes.
        "norm_full": format_duration(d.norm_full), "fact_full": format_duration(d.fact_full),
        "debt_full": format_duration(d.debt_full), "norm_lock": format_duration(d.norm_lock),
        "fact_lock": format_duration(d.fact_lock), "debt_lock": format_duration(d.debt_lock),
        "make_up_wh": format_duration(d.make_up_wh), "locked_for_compensation": d.locked_for_compensation,
        "note": d.note,
    }


def _ser_compensation(c):
    # Frontend uchun BIR XIL days[]/durations[] tuzilishi (HH:MM:SS, bir tartibda):
    #   - otrabotka        → CompensationDay (work-off kunlari va per-day davomiylik);
    #   - auto-approve      → CompensationDay yo'q, shuning uchun days=[planned_date],
    #     (debt-justification)  durations=[verified_duration] bilan to'ldiramiz.
    comp_days = list(c.days.all().order_by("day"))
    if comp_days:
        days = [_iso(d.day) for d in comp_days]
        durations = [format_duration(d.allocated_duration) for d in comp_days]
    else:
        days = [_iso(c.planned_date)] if c.planned_date else []
        durations = [format_duration(c.verified_duration)] if c.verified_duration else []
    return {
        "external_id": c.id, "operator_id": c.operator_id, "cycle_id": c.cycle_id, "status": c.status,
        "comment": c.comment, "verified_at": c.verified_at.isoformat() if c.verified_at else None,
        "verified_by_id": c.verified_by_id, "fixed_by_id": c.fixed_by_id, "type_rule_id": c.type_rule_id,
        "source": c.source, "planned_date": _iso(c.planned_date),
        "requested_duration": _min(c.requested_duration) if c.requested_duration else None,
        "verified_duration": _min(c.verified_duration) if c.verified_duration else None,
        "remaining_debt": _min(c.remaining_debt) if c.remaining_debt else None,
        "deducted": c.deducted, "correlation_id": getattr(c, "correlation_id", None),
        # WFM shu maydonlarni ko'rsatadi — days va durations (HH:MM:SS) bir xil tartibda.
        "days": days,
        "durations": durations,
    }


def _ser_transfer(t):
    return {
        "external_id": t.id, "operator_id": t.operator_id, "cycle_id": t.cycle_id, "status": t.status,
        "comment": t.comment, "type_rule_id": t.type_rule_id,
        "date_from": _iso(t.date_from), "date_to": _iso(t.date_to),
        "hour_from": _hhmm(t.hour_from), "hour_to": _hhmm(t.hour_to),
        "requested_duration": _min(t.requested_duration) if t.requested_duration else None,
        "verified_duration": _min(t.verified_duration) if t.verified_duration else None,
        "remaining_debt": _min(t.remaining_debt) if t.remaining_debt else None,
        "correlation_id": getattr(t, "correlation_id", None),
    }


def _ser_debt_link(l):
    return {
        "external_id": l.id, "compensation_id": l.compensation_id, "debt_detail_id": l.debt_detail_id,
        "snapshot": l.snapshot or {}, "applied": l.applied,
        "applied_amount": _min(l.applied_amount) if l.applied_amount else None,
    }


def push_master_data(since=None) -> dict:
    """Reference data owned by Python: only records changed since the watermark."""
    out = {
        "operator-groups": _push("operator-groups", [_ser_group(x) for x in _since_filter(Group.objects.all(), since)]),
        "shifts": _push("shifts", [_ser_shift(x) for x in _since_filter(Shift.objects.all(), since)]),
        "type-rules": _push("type-rules", [_ser_type_rule(x) for x in _since_filter(RequestTypeRule.objects.all(), since)]),
        "operators": _push("operators", [_ser_operator(x) for x in _since_filter(Operator.objects.all(), since)]),
    }
    logger.info("WFM master push (since=%s): %s", since, out)
    return out


def push_cycle_to_wfm(cycle=None, include_master=True, full=False) -> dict:
    """
    Push changed records (since last watermark) for a cycle + master data. Call at runner end.
    full=True => ignore watermark and push everything (use for a one-time backfill).
    """
    started_at = timezone.now()
    since = None if full else _load_watermark()

    if cycle is None:
        cycle = Cycle.get_active()
    out = {"since": since.isoformat() if since else None}
    if include_master:
        out["master"] = push_master_data(since)
    if cycle is None:
        logger.warning("WFM push: aktiv cikl topilmadi")
        return out

    wfm_client.push("cycles", [_ser_cycle(cycle)])  # cycle: bir nechta qator, doim
    out["work-debts"] = _push("work-debts", [_ser_work_debt(x) for x in _since_filter(WorkDebt.objects.filter(cycle=cycle), since)])
    out["work-debt-details"] = _push("work-debt-details", [_ser_detail(x) for x in _since_filter(WorkDebtDetail.objects.filter(cycle=cycle), since)])
    out["compensations"] = _push("compensations", [_ser_compensation(x) for x in _since_filter(Compensation.objects.filter(cycle=cycle), since)])
    out["transfers"] = _push("transfers", [_ser_transfer(x) for x in _since_filter(Transfer.objects.filter(cycle=cycle), since)])
    out["compensation-debt-links"] = _push(
        "compensation-debt-links",
        [_ser_debt_link(x) for x in _since_filter(CompensationDebtLink.objects.filter(compensation__cycle=cycle), since)],
    )

    # Delete-sync (reconcile): WFM'da bu sikl uchun Django'da endi mavjud bo'lmagan
    # yozuvlarni prune qilamiz. Bu upsert-only push qoldirgan orphanlarni (delete+
    # create churn'idan qolgan eski PK'lar + haqiqiy o'chirilganlar) tozalaydi.
    comp_ids = list(Compensation.objects.filter(cycle=cycle).values_list("id", flat=True))
    out["reconcile"] = {
        "work-debts": _reconcile("work-debts", WorkDebt.objects.filter(cycle=cycle), cycle_id=cycle.id),
        "work-debt-details": _reconcile("work-debt-details", WorkDebtDetail.objects.filter(cycle=cycle), cycle_id=cycle.id),
        "compensations": _reconcile("compensations", Compensation.objects.filter(cycle=cycle), cycle_id=cycle.id),
        "transfers": _reconcile("transfers", Transfer.objects.filter(cycle=cycle), cycle_id=cycle.id),
        "compensation-debt-links": _reconcile(
            "compensation-debt-links",
            CompensationDebtLink.objects.filter(compensation__cycle=cycle),
            compensation_ids=comp_ids),
    }

    _save_watermark(started_at)  # keyingi push shu vaqtdan boshlaydi
    logger.info("WFM push (cycle %s, since=%s): %s", cycle.id, since, out)
    return out


def push_cycle_status(cycle) -> dict:
    """Lighter push for month_runner: only the cycle row."""
    if cycle is None:
        return {"pushed": 0}
    wfm_client.push("cycles", [_ser_cycle(cycle)])
    return {"cycle_id": cycle.id, "status": cycle.status}


def push_after_apply(operator_ids, cycle) -> dict:
    """
    Variant A1: ta'sirlangan operator(lar)ning JORIY to'liq holatini WFM'ga darhol
    (sinxron) push qiladi — 5 ta model:
        operator, work-debt (umumiy qarz), work-debt-detail (qarz detali),
        compensation, transfer  (+ compensation-debt-link).
    Shunda WFM Mongo darhol yangilanadi va GET'lar zudlik bilan ko'rsatadi.

    CHAQIRILADI: apply-request (accept_*) dan keyin VA WFM->Django upsert/delete
    (o'sha 5 entity'dan biri) dan keyin. Xato bo'lsa (WFM o'chgan) asosiy amalni
    buzmaydi — yozuv Django'da bor, keyingi kunlik push baribir sinxronlaydi.
    """
    ops = list({int(o) for o in operator_ids})
    if not ops:
        return {"pushed": False}
    # 1) Operator (sikldan mustaqil)
    out = {
        "operators": _push("operators", [
            _ser_operator(x) for x in Operator.objects.filter(id__in=ops)]),
    }
    # 2) Sikl-scoped: umumiy qarz, qarz detallari, compensationlar, transferlar, linklar
    if cycle is not None:
        out["work-debts"] = _push("work-debts", [
            _ser_work_debt(x) for x in WorkDebt.objects.filter(operator_id__in=ops, cycle=cycle)])
        out["work-debt-details"] = _push("work-debt-details", [
            _ser_detail(x) for x in WorkDebtDetail.objects.filter(operator_id__in=ops, cycle=cycle)])
        out["compensations"] = _push("compensations", [
            _ser_compensation(x) for x in Compensation.objects.filter(operator_id__in=ops, cycle=cycle)])
        out["transfers"] = _push("transfers", [
            _ser_transfer(x) for x in Transfer.objects.filter(operator_id__in=ops, cycle=cycle)])
        out["compensation-debt-links"] = _push("compensation-debt-links", [
            _ser_debt_link(x) for x in CompensationDebtLink.objects.filter(
                compensation__operator_id__in=ops, compensation__cycle=cycle)])
        # Delete-sync (reconcile): tahrirlash link'larni churn qiladi (revert
        # delete + reapply create → yangi PK). Upsert-only push eski PK'larni
        # WFM'da qoldiradi → debt_detail_ids dublikat bo'lib o'sadi. Shu operator(lar)
        # compensationlari bo'yicha WFM'dagi ortiqcha link'larni prune qilamiz.
        comp_ids = list(Compensation.objects.filter(
            operator_id__in=ops, cycle=cycle).values_list("id", flat=True))
        out["reconcile"] = {
            "compensation-debt-links": _reconcile(
                "compensation-debt-links",
                CompensationDebtLink.objects.filter(
                    compensation__operator_id__in=ops, compensation__cycle=cycle),
                compensation_ids=comp_ids),
        }
    logger.info("push_after_apply (operators=%s): %s", ops, out)
    return out
