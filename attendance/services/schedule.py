"""
Bugun jadval bo'yicha ishlashi kerak bo'lgan operatorlar.

Hozirgi model: OperatorScheduleDay (har (operator, kun) uchun bitta yozuv,
shift FK + is_day_off). Eski OperatorSchedule (days dict) endi yo'q.
"""
from datetime import date

from hourly_locks.models import OperatorScheduleDay


def get_today_scheduled_operators():
    """
    Bugun ish kuni bo'lgan (is_day_off=False, shift bor) operatorlar.
    Guruh cheklovi yo'q — HAMMA group ruxsat etilgan (operator biror group'da
    bo'lsa yetarli). Har operatorga `_schedule_value` ("boshlanish-tugash",
    masalan "9-18") biriktiriladi — attendance.parse_schedule shu formatni kutadi.
    """
    today = date.today()

    qs = (
        OperatorScheduleDay.objects
        .filter(
            day=today,
            is_day_off=False,
            shift__isnull=False,
            operator__group__isnull=False,
        )
        .select_related("operator", "operator__group", "shift")
    )

    result = []
    for sd in qs:
        shift = sd.shift
        if not shift or not shift.start_time or not shift.end_time:
            continue
        op = sd.operator
        # For parsing (start/end hours -> scheduled_start, night-shift detection).
        op._schedule_value = f"{shift.start_time.hour}-{shift.end_time.hour}"
        # For display: the real shift code (e.g. "15-24"), falls back to the pair.
        op._schedule_code = shift.code or op._schedule_value
        result.append(op)

    return result
