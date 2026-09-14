"""
WFM -> Django: аудит-лог действий над заявками (compensation/transfer).

WFM-interceptor при каждом POST/PATCH/GET/DELETE пишет лог в свою Mongo и
real-time push'ит сюда. Идемпотентность — по wfm_id (Mongo _id лога): повторный
push тот же лог не дублирует (update_or_create).

WFM call:  POST {PYTHON}/api/sync/action-log   (header: x-sync-token)
Body: один лог-объект ИЛИ список логов (батч).
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status as http_status
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .service_auth import HasServiceToken, HasServiceTokenOrQuery
from hourly_locks.models import RequestActionLog


def _save_one(d: dict):
    """Один лог → RequestActionLog (upsert по wfm_id). Возвращает (obj|None, error|None)."""
    wfm_id = d.get("wfm_id") or d.get("_id")
    if not wfm_id:
        return None, "wfm_id majburiy"
    created = d.get("created_at")
    try:
        created_dt = parse_datetime(created) if created else None
    except (ValueError, TypeError):
        created_dt = None
    obj, _ = RequestActionLog.objects.update_or_create(
        wfm_id=str(wfm_id),
        defaults={
            "user_id": (d.get("user_id") or "")[:64],
            "user_name": (d.get("user_name") or "")[:255],
            "method": (d.get("method") or "")[:10],
            "entity_type": (d.get("entity_type") or "")[:20],
            "entity_external_id": d.get("entity_external_id"),
            "entity_wfm_id": (d.get("entity_wfm_id") or "")[:64],
            "endpoint": (d.get("endpoint") or "")[:500],
            "payload": d.get("payload") or {},
            "status_code": d.get("status_code"),
            "success": bool(d.get("success", True)),
            "created_at": created_dt or timezone.now(),
        },
    )
    return obj, None


class ActionLogListView(APIView):
    """GET: список аудит-логов с человекочитаемым (RU) `summary`.
    Фильтры: ?method= &entity_type= &entity_external_id= &user_id= &success=
    Пагинация: ?page= &limit= (макс 100).
    Токен: заголовок x-sync-token ИЛИ ?token=… (удобно в браузере)."""
    permission_classes = [HasServiceTokenOrQuery]

    def get(self, request):
        from ..services.action_log_format import format_action_log
        qp = request.query_params
        qs = RequestActionLog.objects.all()
        if qp.get("method"):
            qs = qs.filter(method=qp["method"].upper())
        if qp.get("entity_type"):
            qs = qs.filter(entity_type=qp["entity_type"])
        if qp.get("entity_external_id"):
            qs = qs.filter(entity_external_id=qp["entity_external_id"])
        if qp.get("user_id"):
            qs = qs.filter(user_id=qp["user_id"])
        if qp.get("success") in ("true", "false"):
            qs = qs.filter(success=(qp["success"] == "true"))

        try:
            page = max(int(qp.get("page", 1)), 1)
            limit = min(max(int(qp.get("limit", 20)), 1), 100)
        except ValueError:
            page, limit = 1, 20

        total = qs.count()
        rows = qs[(page - 1) * limit: page * limit]
        data = [{
            "id": l.id, "wfm_id": l.wfm_id,
            "user_id": l.user_id, "user_name": l.user_name,
            "method": l.method, "entity_type": l.entity_type,
            "entity_external_id": l.entity_external_id,
            "endpoint": l.endpoint, "status_code": l.status_code,
            "success": l.success,
            "created_at": l.created_at.isoformat() if l.created_at else None,
            "payload": l.payload,
            "summary": format_action_log(l),
        } for l in rows]
        return Response({
            "data": data, "total": total, "page": page, "limit": limit,
            "pages": (total + limit - 1) // limit if limit else 1,
        })


class ActionLogIngestView(APIView):
    permission_classes = [HasServiceToken]

    def post(self, request):
        data = request.data
        items = data if isinstance(data, list) else [data]
        saved, errors = 0, []
        for d in items:
            if not isinstance(d, dict):
                errors.append("элемент не объект")
                continue
            obj, err = _save_one(d)
            if err:
                errors.append(err)
            else:
                saved += 1
        return Response(
            {"ok": not errors, "saved": saved, "errors": errors},
            status=http_status.HTTP_200_OK if saved else http_status.HTTP_400_BAD_REQUEST,
        )
