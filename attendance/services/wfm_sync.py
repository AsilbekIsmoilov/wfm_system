"""
Kunlik davomatni (AttendanceLog) WFM'ga saqlash uchun push.

Django har kuni (kun oxirida) bugungi AttendanceLog'ni WFM'ga yuboradi
(/sync/ingest/attendance-logs). WFM `attendance_logs` kolleksiyasida saqlaydi —
tarix `date` bo'yicha yig'iladi: kim nechida keldi (login_time), kim kech qoldi
(status/delay). Upsert kaliti = external_id (Django AttendanceLog.id).
"""
from hourly_locks.sync import wfm_client

CHUNK = 500


def _ser(log):
    return {
        "external_id": log.id,
        "operator_id": log.operator_id,
        "date": log.date.isoformat() if log.date else None,
        "scheduled_start": log.scheduled_start.isoformat() if log.scheduled_start else None,
        "login_time": log.login_time.isoformat() if log.login_time else None,
        "status": log.status,
        "delay_minutes": log.delay_minutes or 0,
        "comment": log.comment or "",
    }


def push_attendance_day(target_date) -> int:
    """target_date kunidagi barcha AttendanceLog'ni WFM'ga push qiladi (upsert)."""
    from attendance.models import AttendanceLog
    items = [_ser(x) for x in AttendanceLog.objects.filter(date=target_date).select_related(None)]
    for i in range(0, len(items), CHUNK):
        wfm_client.push("attendance-logs", items[i:i + CHUNK])
    return len(items)
