from datetime import timedelta, datetime
from django.utils import timezone
from attendance.models import AttendanceLog

from .api import fetch_operator_snapshot
from .schedule import get_today_scheduled_operators
from .parser import parse_schedule, calculate_login_time


EARLY_LOGIN_MINUTES = 30
LATE_WINDOW_MINUTES = 30
ON_TIME_GRACE_MINUTES = 1


def ensure_aware(dt):
    if dt is None:
        return None

    current_tz = timezone.get_current_timezone()

    if timezone.is_naive(dt):
        return timezone.make_aware(dt, current_tz)

    return timezone.localtime(dt, current_tz)


def normalize_login(value):
    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    return value


def is_night_shift(start_hour, end_hour):
    return end_hour < start_hour


def resolve_work_date(now, start_hour, end_hour):
    now = ensure_aware(now)
    work_date = now.date()

    if is_night_shift(start_hour, end_hour):
        if now.hour < end_hour:
            work_date -= timedelta(days=1)

    return work_date


def resolve_live_status(now, scheduled_start, log):
    if log and log.login_time:
        return log.status

    late_start = scheduled_start + timedelta(minutes=ON_TIME_GRACE_MINUTES)
    window_end = scheduled_start + timedelta(minutes=LATE_WINDOW_MINUTES)

    if now <= late_start:
        return None
    elif now <= window_end:
        return "lating"
    else:
        return "absent"


def build_scheduled_start(work_date, start_hour):
    dt = datetime.combine(
        work_date,
        datetime.min.time()
    ).replace(
        hour=start_hour,
        minute=0,
        second=0,
        microsecond=0,
    )

    return ensure_aware(dt)


def normalize_login_time(login_time):
    return ensure_aware(login_time)


def upsert_attendance_log(
    operator,
    work_date,
    scheduled_start,
    status,
    login_time=None,
    delay_minutes=0,
):
    scheduled_start = ensure_aware(scheduled_start)
    login_time = normalize_login_time(login_time)

    obj, created = AttendanceLog.objects.get_or_create(
        operator=operator,
        date=work_date,
        defaults={
            "scheduled_start": scheduled_start,
            "login_time": login_time,
            "status": status,
            "delay_minutes": delay_minutes,
        }
    )

    if not created:
        updated_fields = []

        if obj.scheduled_start != scheduled_start:
            obj.scheduled_start = scheduled_start
            updated_fields.append("scheduled_start")

        if obj.status == "absent" and login_time:
            obj.status = status
            obj.login_time = login_time
            obj.delay_minutes = delay_minutes

            updated_fields.extend([
                "status",
                "login_time",
                "delay_minutes",
            ])

        elif obj.login_time is None and login_time:
            obj.login_time = login_time
            obj.status = status
            obj.delay_minutes = delay_minutes

            updated_fields.extend([
                "login_time",
                "status",
                "delay_minutes",
            ])

        if updated_fields:
            obj.save(update_fields=list(set(updated_fields)))

    return obj


def process_attendance(now=None):
    if not now:
        now = timezone.localtime()

    now = ensure_aware(now)

    api_data = fetch_operator_snapshot()
    agents = api_data.get("data", {}).get("agents", [])

    api_map = {
        normalize_login(a.get("login")): a
        for a in agents
        if normalize_login(a.get("login"))
    }

    scheduled_ops = [
        op for op in get_today_scheduled_operators()
        if getattr(op, "role", None) == "operator"
    ]

    for op in scheduled_ops:
        try:
            login = normalize_login(op.login_id)
            if not login:
                continue

            parsed = parse_schedule(op._schedule_value)
            if not parsed:
                continue

            start_hour, end_hour = parsed

            work_date = resolve_work_date(now, start_hour, end_hour)
            scheduled_start = build_scheduled_start(work_date, start_hour)

            window_start = scheduled_start - timedelta(minutes=EARLY_LOGIN_MINUTES)
            window_end = scheduled_start + timedelta(minutes=LATE_WINDOW_MINUTES)

            existing = AttendanceLog.objects.filter(
                operator=op,
                date=work_date
            ).first()

            api_item = api_map.get(login)

            if now < window_start:
                continue

            if api_item:
                duration = int(api_item.get("agentStateDuration", 0))
                login_time = calculate_login_time(now, duration)
                login_time = normalize_login_time(login_time)

                if not login_time:
                    continue

                if login_time < window_start:
                    continue

                delay = int(
                    (login_time - scheduled_start).total_seconds() // 60
                )

                status = "on_time" if delay <= 1 else "late"

                upsert_attendance_log(
                    operator=op,
                    work_date=work_date,
                    scheduled_start=scheduled_start,
                    status=status,
                    login_time=login_time,
                    delay_minutes=max(delay, 0),
                )

            else:
                if now > window_end and not existing:
                    upsert_attendance_log(
                        operator=op,
                        work_date=work_date,
                        scheduled_start=scheduled_start,
                        status="absent",
                        login_time=None,
                        delay_minutes=0,
                    )

        except Exception as e:
            print(f"[ATTENDANCE ERROR] {op.id}: {e}")


def build_attendance(now=None):
    if not now:
        now = timezone.localtime()

    now = ensure_aware(now)

    scheduled_ops = [
        op for op in get_today_scheduled_operators()
        if getattr(op, "role", None) == "operator"
    ]

    today = now.date()
    yesterday = today - timedelta(days=1)

    # role — Operator'da property (DB maydon emas), shuning uchun ORM'da filtrlab
    # bo'lmaydi. scheduled_ops allaqachon role bo'yicha filtrlangan.
    logs = AttendanceLog.objects.filter(
        date__in=[today, yesterday]
    )

    log_map = {
        (l.operator_id, l.date): l
        for l in logs
    }

    result = []

    for op in scheduled_ops:
        try:
            login = normalize_login(op.login_id)
            if not login:
                continue

            parsed = parse_schedule(op._schedule_value)
            if not parsed:
                continue

            start_hour, end_hour = parsed

            work_date = resolve_work_date(now, start_hour, end_hour)
            scheduled_start = build_scheduled_start(work_date, start_hour)

            log = log_map.get((op.id, work_date))

            login_time = None
            if log and log.login_time:
                login_time = timezone.localtime(
                    ensure_aware(log.login_time)
                ).strftime("%H:%M:%S")

            status = resolve_live_status(now, scheduled_start, log)

            result.append({
                "login": login,
                "name": f"{op.surname} {op.name} {op.middle_name or ''}".strip(),
                "schedule": op._schedule_value,
                "scheduled_start": scheduled_start.strftime("%H:%M"),
                "status": status,
                "login_time": login_time,
                "delay_minutes": log.delay_minutes if log else None,

                "group": {"id": op.group.id,
                          "name": op.group.name} if op.group else None,

                "photo": None,
            })

        except Exception as e:
            print(f"[BUILD ATTENDANCE ERROR] {op.id}: {e}")

    return result
