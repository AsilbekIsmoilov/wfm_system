import os
from celery import Celery
from celery.schedules import crontab
# import django
# django.setup()


os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

app = Celery('core')

app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()


app.conf.beat_schedule = {
    "attendance-every-10-sec": {
        "task": "attendance.tasks.attendance_worker",
        "schedule": 10.0,
    },
    # Kunlik davomatni WFM'ga saqlash (soat 23:00, tarix uchun).
    "attendance-daily-save": {
        "task": "attendance.tasks.push_attendance_to_wfm",
        "schedule": crontab(hour=23, minute=0),
    },
    "daily-pipeline": {
        "task": "hourly_locks.daily_pipeline",
        "schedule": crontab(hour=9, minute=0),
    },

    "monthly-close": {
        "task": "hourly_locks.auto_close_cycle",
        "schedule": crontab(hour=23, minute=30, day_of_month=20),
    },
}