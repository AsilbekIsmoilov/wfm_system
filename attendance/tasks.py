from celery import shared_task
from django.utils import timezone
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from .services.attendance import build_attendance, process_attendance


@shared_task
def attendance_worker():
    now = timezone.localtime()

    print("[CELERY] update:", now)

    process_attendance(now)
    data = build_attendance(now)

    print("[CELERY] data count:", len(data))

    channel_layer = get_channel_layer()

    async_to_sync(channel_layer.group_send)(
        "attendance_group",
        {
            "type": "send_attendance",
            "data": data,
        }
    )

    print("[CELERY] group_send done")

    # Shu snapshotni WFM'ga ham uzatamiz (har 10s, faqat ko'rsatish uchun).
    # Xato bo'lsa (WFM o'chgan) task buzilmaydi.
    try:
        from hourly_locks.sync import wfm_client
        wfm_client.push_attendance_live({"fetchedAt": now.isoformat(), "records": data})
        print("[CELERY] WFM attendance push done")
    except Exception as e:
        print("[CELERY] WFM attendance push failed:", e)


@shared_task
def push_attendance_to_wfm(target_date=None):
    """
    Kunlik davomatni WFM'ga SAQLAB qo'yish (persisted). Kun oxirida (beat) yoki
    qo'lda ishga tushiriladi. target_date berilmasa — bugungi kun.
    """
    from datetime import date as _date
    from django.utils import timezone
    from attendance.services.wfm_sync import push_attendance_day
    d = _date.fromisoformat(target_date) if target_date else timezone.localdate()
    n = push_attendance_day(d)
    print(f"[CELERY] attendance-logs WFM'ga saqlandi: {n} ta (date={d})")
    return {"date": str(d), "pushed": n}
