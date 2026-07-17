"""
Django management команда: первичное наполнение БД базовыми данными.

Заполняет:
  - Shift  — стандартные смены
  - RequestTypeRule — типы заявок
  - SystemPolicy — базовые настройки
  - Cycle — текущий активный цикл

Использование:
  python manage.py seed_initial_data
  python manage.py seed_initial_data --reset  (опасно — удалит существующее)
"""

from datetime import time, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

from hourly_locks.models import (
    RequestTypeRule, RequestSubtypeRule, Shift, SystemPolicy,
)
from hourly_locks.services.cycle import get_or_create_active_cycle


SHIFTS = [
    # code, display_name, start, end, crosses_midnight, norm_full, norm_lock_soft_cap, norm_lock_warn_at, tolerance, is_night, special
    ("06-15", "Утро 06:00-15:00", time(6), time(15), False, timedelta(hours=9), timedelta(hours=1, minutes=46), timedelta(hours=1, minutes=30), timedelta(0), False, False),
    ("07-15", "Утро 07:00-15:00", time(7), time(15), False, timedelta(hours=8), timedelta(hours=1, minutes=46), timedelta(0), timedelta(0), False, False),
    ("07-16", "Утро 07:00-16:00", time(7), time(16), False, timedelta(hours=9), timedelta(hours=1, minutes=46), timedelta(hours=1, minutes=30), timedelta(0), False, False),
    ("08-16", "Утро 08:00-16:00", time(8), time(16), False, timedelta(hours=8), timedelta(hours=1, minutes=46), timedelta(0), timedelta(0), False, False),
    ("08-17", "День 08:00-17:00", time(8), time(17), False, timedelta(hours=9), timedelta(hours=1, minutes=46), timedelta(hours=1, minutes=30), timedelta(0), False, False),
    ("09-17", "День 09:00-17:00", time(9), time(17), False, timedelta(hours=8), timedelta(hours=1, minutes=46), timedelta(0), timedelta(0), False, False),
    ("09-18", "День 09:00-18:00", time(9), time(18), False, timedelta(hours=9), timedelta(hours=1, minutes=46), timedelta(hours=1, minutes=30), timedelta(0), False, False),
    ("10-19", "День 10:00-19:00", time(10), time(19), False, timedelta(hours=9), timedelta(hours=1, minutes=46), timedelta(hours=1, minutes=30), timedelta(0), False, False),
    ("11-20", "День 11:00-20:00", time(11), time(20), False, timedelta(hours=9), timedelta(hours=1, minutes=46), timedelta(hours=1, minutes=30), timedelta(minutes=10), False, False),
    ("13-22", "Вечер 13:00-22:00", time(13), time(22), False, timedelta(hours=9), timedelta(hours=1, minutes=46), timedelta(hours=1, minutes=30), timedelta(minutes=10), False, False),
    ("15-24", "Вечер 15:00-00:00", time(15), time(0), True, timedelta(hours=9), timedelta(hours=1, minutes=46), timedelta(hours=1, minutes=30), timedelta(minutes=10), True, False),
    ("17-02", "Ночь 17:00-02:00", time(17), time(2), True, timedelta(hours=9), timedelta(hours=1, minutes=46), timedelta(hours=1, minutes=30), timedelta(0), True, False),
    ("18-03", "Ночь 18:00-03:00", time(18), time(3), True, timedelta(hours=9), timedelta(hours=1, minutes=46), timedelta(hours=1, minutes=30), timedelta(0), True, False),
    ("08-20", "Дневная 12ч 08:00-20:00", time(8), time(20), False, timedelta(hours=12), timedelta(hours=2, minutes=20), timedelta(0), timedelta(minutes=10), False, False),
    ("20-08", "Ночная 12ч 20:00-08:00", time(20), time(8), True, timedelta(hours=12), timedelta(hours=2, minutes=20), timedelta(0), timedelta(0), True, True),
]


# Все типы (кроме otrabotka) — в ОБЕИХ категориях (compensation + transfer).
# otrabotka — только в compensation.
_REQUEST_TYPES = [
    # (code, display_name, group)  group: "work" | "benefit"
    ("otrabotka",       "Отработка",                     "work"),
    ("otprashivanie",   "Отпрашивание",                  "work"),
    ("isklyuchenie",    "Исключение",                    "benefit"),
    ("ne_otrabotaet",   "Не отработает",                 "benefit"),
    # Учёба — НЕ benefit: норма не уменьшается, проверяется по факту
    # (расширенное окно, services.ucheba), долг если норма не покрыта.
    ("ucheba_rabochee", "Учёба в рабочее время",         "work"),
    ("perenos_dnya",    "Перенос рабочего дня",          "work"),
    ("training_on_duty",   "Обучение на службу",         "benefit"),
    ("training_on_errors", "Обучение по ошибкам",        "benefit"),
    ("lgota",           "Льгота",                        "benefit"),
    ("lgota_brak",      "Льгота по бракосочетанию",      "benefit"),
    ("lgota_rojdenie",  "Льгота по рождению ребёнка",    "benefit"),
    ("lgota_utrata",    "Льгота по утрате родственника", "benefit"),
    ("lgota_pereezd",   "Льгота по переезду",            "benefit"),
    ("lgota_prochie",   "Прочие льготы",                 "benefit"),
    ("hoz_raboty",      "Хозяйственные работы",          "benefit"),
]

# Подтипы заявок: type_code -> [ {code, display_name, full_day,
# requires_hour_range, sort_order}, ... ]. Привязаны к RequestTypeRule.
SUBTYPE_RULES = {
    "lgota_prochie": [
        dict(code="otgul", display_name="Отгул",
             full_day=True, requires_hour_range=False, sort_order=10),
        dict(code="korotkiy_den", display_name="Короткий день",
             full_day=False, requires_hour_range=True, sort_order=20),
    ],
}
_WORK_FLAGS = dict(
    creates_debt_if_unmet=True, exempts_from_daily_debt=False,
    auto_approve_on_create=False, allows_past_date=False,
    verification_strategy="schedule_based",
)
_BENEFIT_FLAGS = dict(
    creates_debt_if_unmet=False, exempts_from_daily_debt=True,
    auto_approve_on_create=True, allows_past_date=True,
    verification_strategy="auto_approve",
)
# otrabotka и ne_otrabotaet — ТОЛЬКО в compensation (из transfer исключены).
_COMPENSATION_ONLY = {"otrabotka", "ne_otrabotaet"}
REQUEST_TYPE_RULES = [
    dict(category=cat, code=code, display_name=name, sort_order=idx * 10,
         **(_WORK_FLAGS if grp == "work" else _BENEFIT_FLAGS))
    for cat in ("compensation", "transfer")
    for idx, (code, name, grp) in enumerate(_REQUEST_TYPES, start=1)
    if not (cat == "transfer" and code in _COMPENSATION_ONLY)
]


SYSTEM_POLICIES = [
    dict(
        key="external_api_url",
        value="http://192.168.42.172:5000/csv-to-json/hour_by",
        description="URL внешнего API почасовых логов",
    ),
    dict(
        key="external_api_timeout_seconds",
        value=30,
        description="Таймаут запросов к внешнему API (секунд)",
    ),
    dict(
        key="cycle_close_day",
        value=20,
        description="День месяца для закрытия цикла",
    ),
    dict(
        key="compensation_tolerance_minutes",
        value=5,
        description="Допуск при сравнении плана и факта компенсации (минут)",
    ),
    dict(
        key="allowed_groups",
        value=[
            "1000", "1009", "1093", "1170", "1242",
            "BKM", "INT", "Группа ДОП", "ДОП",
            "Саралаш", "РФ", "Продажа",
            "Нукус 1000", "229 (1000)", "БКМ", "БКМ/09",
            "ДОП 1", "ДОП 2",
        ],
        description="Список разрешённых групп для операций",
    ),
    dict(
        key="google_credential_path",
        value="/home/projects/hour_by_project/creds.json",
        description="Путь к JSON-ключу Google Service Account",
    ),
    dict(
        key="auto_create_operators_from_api",
        value=True,
        description=(
            "Авто-создавать новых операторов, появившихся в API. "
            "True (по умолчанию) — создавать с пустой группой и логировать. "
            "False — игнорировать неизвестные login_id."
        ),
    ),
]


class Command(BaseCommand):
    help = "Первичное наполнение БД базовыми данными"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset", action="store_true",
            help="Удалить существующие записи перед заливкой (ОПАСНО)",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if options["reset"]:
            self.stdout.write(self.style.WARNING("Сброс старых данных..."))
            Shift.objects.all().delete()
            RequestTypeRule.objects.all().delete()
            SystemPolicy.objects.all().delete()

        self.stdout.write("Создание смен...")
        for s in SHIFTS:
            (
                code, display_name, start, end, crosses, norm_full,
                norm_lock_soft, norm_lock_warn, tolerance, is_night, special,
            ) = s
            Shift.objects.update_or_create(
                code=code,
                defaults={
                    "display_name": display_name,
                    "start_time": start,
                    "end_time": end,
                    "crosses_midnight": crosses,
                    "norm_full": norm_full,
                    "norm_lock_soft_cap": norm_lock_soft,
                    "norm_lock_warn_at": norm_lock_warn,
                    "tolerance_undertime": tolerance,
                    "is_night": is_night,
                    "requires_special_pipeline": special,
                    "is_active": True,
                },
            )
        self.stdout.write(self.style.SUCCESS(f"Смены: {len(SHIFTS)}"))

        self.stdout.write("Создание правил заявок...")
        for rule_data in REQUEST_TYPE_RULES:
            # is_active is NOT seeded — admins manage it in Django. New rules take the
            # model default (True); existing rules keep whatever the admin set.
            RequestTypeRule.objects.update_or_create(
                category=rule_data["category"],
                code=rule_data["code"],
                defaults={**rule_data},
            )
        self.stdout.write(self.style.SUCCESS(f"Правила заявок: {len(REQUEST_TYPE_RULES)}"))

        self.stdout.write("Создание подтипов заявок...")
        n_sub = 0
        for type_code, subs in SUBTYPE_RULES.items():
            rule = RequestTypeRule.objects.filter(
                category="transfer", code=type_code).first()
            if not rule:
                continue
            for s in subs:
                RequestSubtypeRule.objects.update_or_create(
                    type_rule=rule, code=s["code"],
                    defaults={k: v for k, v in s.items() if k != "code"},
                )
                n_sub += 1
        self.stdout.write(self.style.SUCCESS(f"Подтипы заявок: {n_sub}"))

        self.stdout.write("Создание системных политик...")
        # СОЗДАЁМ только отсутствующие политики (get_or_create), НЕ перезаписываем
        # существующие: значения, настроенные на проде (api_url, allowed_groups,
        # google_credential_path и т.п.), сохраняются при каждом запуске seed.
        for policy_data in SYSTEM_POLICIES:
            SystemPolicy.objects.get_or_create(
                key=policy_data["key"],
                defaults={
                    "value": policy_data["value"],
                    "description": policy_data["description"],
                },
            )
        self.stdout.write(self.style.SUCCESS(f"Политики: {len(SYSTEM_POLICIES)}"))

        self.stdout.write("Создание активного цикла...")
        cycle = get_or_create_active_cycle()
        self.stdout.write(self.style.SUCCESS(f"Цикл: {cycle}"))

        self.stdout.write(self.style.SUCCESS("Первичное наполнение завершено."))
