from datetime import date, timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

class User(AbstractUser):
    ROLE_CHOICES = [
        ("operator", "Оператор"),
        ("supervisor", "Супервайзер"),
        ("manager", "Менеджер"),
        ("admin", "Администратор"),
    ]
    role = models.CharField(
        verbose_name="Роль",
        max_length=20,
        choices=ROLE_CHOICES,
        default="operator",
        db_index=True,
    )
    operator = models.OneToOneField(
        "hourly_locks.Operator",
        verbose_name="Связанный оператор",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="user_account",
    )

    class Meta:
        verbose_name = "Пользователь"
        verbose_name_plural = "Пользователи"
        indexes = [
            models.Index(fields=["role"]),
        ]

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"


class Group(models.Model):
    name = models.CharField(
        verbose_name="Название",
        max_length=100,
        unique=True,
        db_index=True,
    )
    supervisor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Супервайзер",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supervised_groups",
        limit_choices_to={"role": "supervisor"},
    )
    is_active = models.BooleanField(
        verbose_name="Активна",
        default=True,
        db_index=True,
    )
    is_excluded = models.BooleanField(
        verbose_name="Исключена из обработки",
        default=False,
        db_index=True,
        help_text=(
            "True — операторы этой группы полностью пропускаются: не считается "
            "долг, не грузятся логи, нет посещаемости, нельзя подать заявку. "
            "Единый переключатель для всех подсистем (deny-list)."
        ),
    )
    created_at = models.DateTimeField(
        verbose_name="Создана",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        verbose_name="Обновлена",
        auto_now=True,
    )

    class Meta:
        verbose_name = "Группа операторов"
        verbose_name_plural = "Группы операторов"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Operator(models.Model):
    login_id = models.CharField(
        verbose_name="Логин",
        max_length=50,
        unique=True,
        db_index=True,
    )
    surname = models.CharField(
        verbose_name="Фамилия",
        max_length=150,
        db_index=True,
    )
    name = models.CharField(
        verbose_name="Имя",
        max_length=150,
        db_index=True,
    )
    middle_name = models.CharField(
        verbose_name="Отчество",
        max_length=150,
        blank=True,
        null=True,
        db_index=True,
    )
    group = models.ForeignKey(
        "hourly_locks.Group",
        verbose_name="Группа",
        related_name="operators",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
    )
    is_active = models.BooleanField(
        verbose_name="Активен",
        default=True,
        db_index=True,
        help_text="Для уволенных сотрудников — False",
    )
    created_at = models.DateTimeField(
        verbose_name="Создан",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        verbose_name="Обновлён",
        auto_now=True,
    )

    class Meta:
        verbose_name = "Оператор"
        verbose_name_plural = "Операторы"
        ordering = ["surname", "name"]
        indexes = [
            models.Index(fields=["group"]),
            models.Index(fields=["group", "login_id"]),
            models.Index(fields=["surname", "name"]),
            models.Index(fields=["is_active"]),
        ]

    @property
    def full_name(self) -> str:
        return f"{self.surname} {self.name} {self.middle_name or ''}".strip()

    @property
    def role(self) -> str:
        user = getattr(self, "user_account", None)
        return user.role if user else "operator"

    def __str__(self):
        return f"{self.full_name} ({self.login_id})"

class Shift(models.Model):
    code = models.CharField(
        verbose_name="Код",
        max_length=20,
        unique=True,
        db_index=True,
        help_text="Например: '08-20', '20-08', '11-20'",
    )
    display_name = models.CharField(
        verbose_name="Название",
        max_length=100,
    )

    start_time = models.TimeField(
        verbose_name="Начало смены",
    )
    end_time = models.TimeField(
        verbose_name="Окончание смены",
    )
    crosses_midnight = models.BooleanField(
        verbose_name="Через полночь",
        default=False,
        help_text="Ночная смена, переходящая через полночь",
    )

    norm_full = models.DurationField(
        verbose_name="Норма рабочего времени",
        help_text="Например: 12:00 / 9:00 / 8:00",
    )
    norm_lock_soft_cap = models.DurationField(
        verbose_name="Норма перерыва (мягкая)",
        help_text="Например: 2:20 или 1:46",
    )
    norm_lock_warn_at = models.DurationField(
        verbose_name="Порог штрафа за перерыв",
        default=timedelta(0),
        help_text="Например: 1:30 для 9-часовой смены. 0 = не используется.",
    )

    tolerance_undertime = models.DurationField(
        verbose_name="Допуск недоработки",
        default=timedelta(0),
        help_text="Например: 10 минут — недоработка в пределах допуска не создаёт долг",
    )

    fetch_hour_padding = models.PositiveSmallIntegerField(
        verbose_name="Расширение часов при загрузке",
        default=1,
        help_text="На сколько часов расширять окно при загрузке логов с API",
    )

    is_night = models.BooleanField(
        verbose_name="Ночная",
        default=False,
    )
    requires_special_pipeline = models.BooleanField(
        verbose_name="Требует спец. конвейера",
        default=False,
        help_text="True для смены 20-08 (специальный ночной конвейер)",
    )

    is_active = models.BooleanField(
        verbose_name="Активна",
        default=True,
        db_index=True,
    )

    created_at = models.DateTimeField(
        verbose_name="Создана",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        verbose_name="Обновлена",
        auto_now=True,
    )

    class Meta:
        verbose_name = "Смена"
        verbose_name_plural = "Смены"
        ordering = ["code"]
        indexes = [
            models.Index(fields=["is_active"]),
            models.Index(fields=["code", "is_active"]),
        ]

    def __str__(self):
        return f"{self.code} — {self.display_name}"


class OperatorScheduleDay(models.Model):
    SOURCE_CHOICES = [
        ("sheets", "Google Sheets"),
        ("manual", "Вручную"),
        ("system", "Системно"),
    ]

    operator = models.ForeignKey(
        "Operator",
        verbose_name="Оператор",
        on_delete=models.CASCADE,
        related_name="schedule_days",
        db_index=True,
    )
    day = models.DateField(
        verbose_name="День",
        db_index=True,
    )
    shift = models.ForeignKey(
        "Shift",
        verbose_name="Смена",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        help_text="Пустое значение = выходной день",
    )
    is_day_off = models.BooleanField(
        verbose_name="Выходной",
        default=False,
    )

    source = models.CharField(
        verbose_name="Источник",
        max_length=20,
        choices=SOURCE_CHOICES,
        default="sheets",
    )
    raw_value = models.CharField(
        verbose_name="Исходное значение",
        max_length=50,
        blank=True,
        null=True,
        help_text="Оригинальное значение из Sheets (для аудита)",
    )

    synced_at = models.DateTimeField(
        verbose_name="Синхронизирован",
        auto_now=True,
    )
    created_at = models.DateTimeField(
        verbose_name="Создан",
        auto_now_add=True,
    )

    class Meta:
        verbose_name = "День расписания оператора"
        verbose_name_plural = "Расписание операторов (по дням)"
        unique_together = ("operator", "day")
        indexes = [
            models.Index(fields=["day"]),
            models.Index(fields=["operator", "day"]),
            models.Index(fields=["day", "is_day_off"]),
        ]

    def clean(self):
        super().clean()
        if self.is_day_off and self.shift is not None:
            raise ValidationError({
                "shift": "Для выходного дня смена должна быть пустой"
            })

    def __str__(self):
        if self.is_day_off:
            return f"{self.operator} {self.day} — выходной"
        shift_code = self.shift.code if self.shift else "Н/Д"
        return f"{self.operator} {self.day} — {shift_code}"


class RequestTypeRule(models.Model):
    CATEGORY_CHOICES = [
        ("compensation", "Компенсация"),
        ("transfer", "Перенос / Отгул"),
    ]
    VERIFICATION_STRATEGY_CHOICES = [
        ("schedule_based", "По расписанию (overtime - over_lock)"),
        ("net_based", "По чистому времени (full - lock)"),
        ("day_off_based", "По выходному (net = credit)"),
        ("date_range_based", "По диапазону дат"),
        ("hour_range_based", "По диапазону часов"),
        ("auto_approve", "Автоматическое одобрение"),
        ("manual_only", "Только вручную"),
        ("night_pipeline", "Ночной конвейер 20-08"),
        ("retroactive_check", "Ретроактивная проверка (по факту)"),
    ]

    category = models.CharField(
        verbose_name="Категория",
        max_length=20,
        choices=CATEGORY_CHOICES,
        db_index=True,
    )
    code = models.CharField(
        verbose_name="Код",
        max_length=30,
        db_index=True,
    )
    display_name = models.CharField(
        verbose_name="Название",
        max_length=100,
    )
    description = models.TextField(
        verbose_name="Описание",
        blank=True,
    )

    requires_date_from = models.BooleanField(
        verbose_name="Требует дату начала",
        default=False,
    )
    requires_date_to = models.BooleanField(
        verbose_name="Требует дату окончания",
        default=False,
    )
    requires_hour_range = models.BooleanField(
        verbose_name="Требует диапазон часов",
        default=False,
    )
    requires_duration = models.BooleanField(
        verbose_name="Требует длительность",
        default=False,
    )
    requires_related_debts = models.BooleanField(
        verbose_name="Требует выбор долгов",
        default=False,
    )

    creates_debt_if_unmet = models.BooleanField(
        verbose_name="Создаёт долг при невыполнении",
        default=True,
        help_text="Если оператор не выполнил — создаётся долг. Для vacation/sl — False.",
    )
    exempts_from_daily_debt = models.BooleanField(
        verbose_name="Освобождает от ежедневного долга",
        default=False,
        help_text="В этот день долг не считается (training, vacation, sl, wc, study).",
    )
    auto_approve_on_create = models.BooleanField(
        verbose_name="Авто-одобрение при создании",
        default=False,
        help_text="Сразу approved при создании (exception, no_compensation).",
    )
    allows_past_date = models.BooleanField(
        verbose_name="Разрешает прошедшие даты",
        default=False,
        help_text="True для ретроактивных заявок.",
    )
    requires_supervisor_approval = models.BooleanField(
        verbose_name="Требует подтверждения супервайзера",
        default=False,
        help_text="Даже после авто-проверки супервайзер должен подтвердить вручную.",
    )
    forbidden_on_day_off = models.BooleanField(
        verbose_name="Запрещён в выходной",
        default=False,
        help_text=(
            "True — заявку нельзя создать, если date_from или date_to "
            "приходится на выходной оператора (без смены)."
        ),
    )

    min_duration = models.DurationField(
        verbose_name="Минимальная длительность",
        null=True,
        blank=True,
    )
    max_duration = models.DurationField(
        verbose_name="Максимальная длительность",
        null=True,
        blank=True,
    )

    verification_strategy = models.CharField(
        verbose_name="Стратегия проверки",
        max_length=50,
        choices=VERIFICATION_STRATEGY_CHOICES,
        default="schedule_based",
    )

    sort_order = models.PositiveSmallIntegerField(
        verbose_name="Порядок сортировки",
        default=100,
    )
    is_active = models.BooleanField(
        verbose_name="Активно",
        default=True,
    )

    created_at = models.DateTimeField(
        verbose_name="Создано",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        verbose_name="Обновлено",
        auto_now=True,
    )

    class Meta:
        verbose_name = "Правило типа заявки"
        verbose_name_plural = "Правила типов заявок"
        unique_together = ("category", "code")
        ordering = ["category", "sort_order", "code"]
        indexes = [
            models.Index(fields=["category", "is_active"]),
            models.Index(fields=["code"]),
        ]

    def __str__(self):
        return f"{self.get_category_display()}: {self.display_name}"


class RequestSubtypeRule(models.Model):
    """Подтип заявки, привязанный к конкретному RequestTypeRule (напр. для
    «Прочие льготы»: Отгул / Короткий день). Поведение — data-driven: full_day и
    requires_hour_range управляют логикой приёма (полный день vs окно часов),
    поэтому новый подтип добавляется через админку без правки кода."""

    type_rule = models.ForeignKey(
        RequestTypeRule,
        verbose_name="Тип заявки",
        on_delete=models.CASCADE,
        related_name="subtypes",
    )
    code = models.CharField(
        verbose_name="Код",
        max_length=30,
        db_index=True,
    )
    display_name = models.CharField(
        verbose_name="Название",
        max_length=100,
    )
    description = models.TextField(
        verbose_name="Описание",
        blank=True,
    )

    full_day = models.BooleanField(
        verbose_name="Полный день",
        default=False,
        help_text="True — отменяется весь рабочий день, часы не нужны (напр. Отгул).",
    )
    requires_hour_range = models.BooleanField(
        verbose_name="Требует диапазон часов",
        default=False,
        help_text="True — нужны hour_from/hour_to (напр. Короткий день).",
    )

    sort_order = models.PositiveSmallIntegerField(
        verbose_name="Порядок сортировки",
        default=100,
    )
    is_active = models.BooleanField(
        verbose_name="Активно",
        default=True,
    )

    created_at = models.DateTimeField(
        verbose_name="Создано",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        verbose_name="Обновлено",
        auto_now=True,
    )

    class Meta:
        verbose_name = "Подтип заявки"
        verbose_name_plural = "Подтипы заявок"
        unique_together = ("type_rule", "code")
        ordering = ["type_rule", "sort_order", "code"]
        indexes = [
            models.Index(fields=["type_rule", "is_active"]),
        ]

    def __str__(self):
        return f"{self.type_rule.display_name} → {self.display_name}"


class SystemPolicy(models.Model):
    key = models.CharField(
        verbose_name="Ключ",
        max_length=100,
        unique=True,
        db_index=True,
    )
    value = models.JSONField(
        verbose_name="Значение",
    )
    description = models.TextField(
        verbose_name="Описание",
        blank=True,
    )

    valid_from = models.DateField(
        verbose_name="Действует с",
        default=date.today,
    )
    valid_to = models.DateField(
        verbose_name="Действует до",
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        verbose_name="Создано",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        verbose_name="Обновлено",
        auto_now=True,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кем обновлено",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "Системная политика"
        verbose_name_plural = "Системные политики"
        ordering = ["key"]

    def __str__(self):
        return f"{self.key} = {self.value}"

class Cycle(models.Model):
    STATUS_CHOICES = [
        ("active", "Активный"),
        ("closing", "Закрывается"),
        ("closed", "Закрытый"),
    ]

    year = models.PositiveSmallIntegerField(
        verbose_name="Год окончания",
    )
    month = models.PositiveSmallIntegerField(
        verbose_name="Месяц окончания",
        help_text="Месяц, в котором цикл заканчивается (1-12)",
    )

    start_date = models.DateField(
        verbose_name="Начало",
        help_text="20-е число предыдущего месяца",
    )
    end_date = models.DateField(
        verbose_name="Окончание",
        help_text="19-е число текущего месяца",
    )

    status = models.CharField(
        verbose_name="Статус",
        max_length=20,
        choices=STATUS_CHOICES,
        default="active",
        db_index=True,
    )

    opened_at = models.DateTimeField(
        verbose_name="Открыт",
        auto_now_add=True,
    )
    closed_at = models.DateTimeField(
        verbose_name="Закрыт",
        null=True,
        blank=True,
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кем закрыт",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="closed_cycles",
    )

    archive_stats = models.JSONField(
        verbose_name="Статистика архивирования",
        default=dict,
        blank=True,
        help_text="Сколько записей перенесено в архив",
    )

    class Meta:
        verbose_name = "Цикл"
        verbose_name_plural = "Циклы"
        unique_together = ("year", "month")
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["year", "month"]),
            models.Index(fields=["start_date", "end_date"]),
        ]
        ordering = ["-year", "-month"]

    @classmethod
    def get_active(cls):
        return cls.objects.filter(status="active").first()

    def contains(self, day: date) -> bool:
        return self.start_date <= day <= self.end_date

    def __str__(self):
        return (
            f"Цикл {self.year}/{self.month:02d} "
            f"({self.start_date} – {self.end_date}) [{self.get_status_display()}]"
        )


class WorkLogDaily(models.Model):
    operator = models.ForeignKey(
        "Operator",
        verbose_name="Оператор",
        on_delete=models.CASCADE,
        related_name="daily_logs",
        db_index=True,
    )
    day = models.DateField(
        verbose_name="День",
        db_index=True,
    )
    cycle = models.ForeignKey(
        "Cycle",
        verbose_name="Цикл",
        on_delete=models.PROTECT,
        related_name="daily_logs",
        db_index=True,
    )

    shift = models.ForeignKey(
        "Shift",
        verbose_name="Смена",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Смена на момент записи лога",
    )
    shift_code_snapshot = models.CharField(
        verbose_name="Код смены (снимок)",
        max_length=20,
        blank=True,
        null=True,
        help_text="Код смены сохраняется даже после удаления Shift",
    )

    start_at = models.DateTimeField(
        verbose_name="Фактическое начало",
        null=True,
        blank=True,
        help_text="Точное время начала смены (для ночных смен)",
    )
    end_at = models.DateTimeField(
        verbose_name="Фактическое окончание",
        null=True,
        blank=True,
    )

    aftercall_duration = models.DurationField(
        verbose_name="После звонка",
        default=timedelta,
    )
    busy_duration = models.DurationField(
        verbose_name="Занято (разговор)",
        default=timedelta,
    )
    hold_duration = models.DurationField(
        verbose_name="Удержание",
        default=timedelta,
    )
    idle_duration = models.DurationField(
        verbose_name="Простой",
        default=timedelta,
    )
    lazy_duration = models.DurationField(
        verbose_name="Без активности",
        default=timedelta,
    )
    lock_duration = models.DurationField(
        verbose_name="Перерыв (lock)",
        default=timedelta,
    )
    relax_duration = models.DurationField(
        verbose_name="Отдых",
        default=timedelta,
    )
    full_duration = models.DurationField(
        verbose_name="Общее время",
        default=timedelta,
    )

    is_special_aggregation = models.BooleanField(
        verbose_name="Спец. агрегация",
        default=False,
        help_text="True для агрегации 20-08 + компенсация",
    )

    loaded_at = models.DateTimeField(
        verbose_name="Загружен",
        auto_now=True,
    )
    created_at = models.DateTimeField(
        verbose_name="Создан",
        auto_now_add=True,
    )

    class Meta:
        verbose_name = "Ежедневный лог"
        verbose_name_plural = "Ежедневные логи"
        unique_together = ("operator", "day")
        indexes = [
            models.Index(fields=["day"]),
            models.Index(fields=["operator", "day"]),
            models.Index(fields=["cycle"]),
            models.Index(fields=["operator", "cycle"]),
        ]

    @property
    def net_duration(self) -> timedelta:
        net = self.full_duration - self.lock_duration
        return net if net > timedelta(0) else timedelta(0)

    def __str__(self):
        return f"{self.operator} – {self.day} ({self.full_duration})"


class WorkDebt(models.Model):
    operator = models.ForeignKey(
        "Operator",
        verbose_name="Оператор",
        on_delete=models.CASCADE,
        related_name="debts",
    )
    cycle = models.ForeignKey(
        "Cycle",
        verbose_name="Цикл",
        on_delete=models.PROTECT,
        related_name="debts",
    )

    current_debt = models.DurationField(
        verbose_name="Текущий долг",
        default=timedelta(0),
    )
    total_accumulated = models.DurationField(
        verbose_name="Накопленный долг",
        default=timedelta(0),
        help_text="Сумма всех начисленных долгов в цикле (не уменьшается)",
    )

    updated_at = models.DateTimeField(
        verbose_name="Обновлён",
        auto_now=True,
    )

    class Meta:
        verbose_name = "Долг оператора"
        verbose_name_plural = "Долги операторов"
        unique_together = ("operator", "cycle")
        indexes = [
            models.Index(fields=["operator"]),
            models.Index(fields=["operator", "cycle"]),
        ]

    @property
    def current_debt_days(self) -> int:
        return int(self.current_debt.total_seconds() // 86400)

    @property
    def current_debt_hhmmss(self) -> str:
        total = int(self.current_debt.total_seconds())
        days = total // 86400
        rem = total - days * 86400
        return f"{rem // 3600:02d}:{(rem % 3600) // 60:02d}:{rem % 60:02d}"

    def __str__(self):
        return f"{self.operator} — {self.current_debt_days} д. {self.current_debt_hhmmss}"


class WorkDebtDetail(models.Model):
    """
    Детализация долга: одна запись = один источник долга за день.
    """
    VIOLATION_TYPE_CHOICES = [
        ("insufficient_wh", "Недоработка"),
        ("exceeding_break", "Превышение перерыва"),
        ("both_violations", "Оба нарушения"),
    ]
    SOURCE_CHOICES = [
        ("shift", "Смена"),
        ("transfer", "Перенос"),
        ("time_off", "Отпрашивание"),
        ("compensation_shortfall", "Недоработанная компенсация"),
        ("nb_compensation_shortfall", "Недоработанная NB-компенсация"),
        ("otrabotka_partial", "Остаток частичной отработки"),
        ("otrabotka_violation", "Нарушение при отработке (норма/перерыв/перебор)"),
        ("compensation_remainder", "Остаток после частичного списания компенсацией"),
        ("ucheba", "Недоработка в день учёбы в рабочее время"),
        ("manual", "Вручную добавлен"),
    ]

    operator = models.ForeignKey(
        "Operator",
        verbose_name="Оператор",
        on_delete=models.CASCADE,
        related_name="debt_details",
        db_index=True,
    )
    day = models.DateField(
        verbose_name="День",
        db_index=True,
    )
    cycle = models.ForeignKey(
        "Cycle",
        verbose_name="Цикл",
        on_delete=models.PROTECT,
        related_name="debt_details",
        db_index=True,
    )

    source = models.CharField(
        verbose_name="Источник",
        max_length=30,
        choices=SOURCE_CHOICES,
        default="shift",
        db_index=True,
    )
    source_object_id = models.PositiveIntegerField(
        verbose_name="ID исходной заявки",
        null=True,
        blank=True,
        help_text="ID связанной Compensation/Transfer",
    )

    shift = models.ForeignKey(
        "Shift",
        verbose_name="Смена",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    shift_code_snapshot = models.CharField(
        verbose_name="Код смены (снимок)",
        max_length=20,
        blank=True,
        null=True,
    )

    violation_type = models.CharField(
        verbose_name="Тип нарушения",
        max_length=30,
        choices=VIOLATION_TYPE_CHOICES,
        null=True,
        blank=True,
        db_index=True,
    )

    norm_full = models.DurationField(
        verbose_name="Норма (полная)",
        default=timedelta,
    )
    fact_full = models.DurationField(
        verbose_name="Факт (полный)",
        default=timedelta,
    )
    debt_full = models.DurationField(
        verbose_name="Долг (полный)",
        default=timedelta,
    )

    norm_lock = models.DurationField(
        verbose_name="Норма перерыва",
        default=timedelta,
    )
    fact_lock = models.DurationField(
        verbose_name="Факт перерыва",
        default=timedelta,
    )
    debt_lock = models.DurationField(
        verbose_name="Долг по перерыву",
        default=timedelta,
    )

    make_up_wh = models.DurationField(
        verbose_name="Компенсирующая переработка",
        default=timedelta,
        help_text="Переработка, компенсирующая превышение перерыва",
    )

    locked_for_compensation = models.BooleanField(
        verbose_name="Привязан к компенсации",
        default=False,
        db_index=True,
        help_text="Привязан к заявке — повторно списать нельзя",
    )

    note = models.TextField(
        verbose_name="Примечание",
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(
        verbose_name="Создан",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        verbose_name="Обновлён",
        auto_now=True,
    )

    class Meta:
        verbose_name = "Детализация долга"
        verbose_name_plural = "Детализация долгов"
        indexes = [
            models.Index(fields=["operator", "day"]),
            models.Index(fields=["cycle"]),
            models.Index(fields=["operator", "cycle"]),
            models.Index(fields=["source"]),
            models.Index(fields=["locked_for_compensation"]),
        ]

    @property
    def total_debt(self) -> timedelta:
        return self.debt_full + self.debt_lock

    def __str__(self):
        return f"{self.operator} – {self.day} [{self.get_source_display()}] — {self.total_debt}"


# =============================================================================
# СЛОЙ 1 (продолжение): Compensation и Transfer (заявки)
# =============================================================================

class BaseWorkRequest(models.Model):
    """
    Абстрактная база. Общие поля Compensation и Transfer.
    """
    STATUS_CHOICES = [
        ("pending", "В ожидании"),
        ("in_progress", "В процессе"),
        ("approved", "Подтверждено"),
        ("partial", "Частично"),
        ("declined", "Отклонено"),
        ("completed", "Завершено"),
    ]

    operator = models.ForeignKey(
        "Operator",
        verbose_name="Оператор",
        on_delete=models.CASCADE,
    )
    cycle = models.ForeignKey(
        "Cycle",
        verbose_name="Цикл",
        on_delete=models.PROTECT,
    )

    status = models.CharField(
        verbose_name="Статус",
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
        db_index=True,
    )
    comment = models.TextField(
        verbose_name="Комментарий",
        blank=True,
        null=True,
    )

    pdf_file = models.FileField(
        verbose_name="PDF файл",
        upload_to="requests/pdf/",
        blank=True,
        null=True,
    )
    screens = models.ImageField(
        verbose_name="Скриншот",
        upload_to="requests/screens/",
        blank=True,
        null=True,
    )

    verified_at = models.DateTimeField(
        verbose_name="Проверено",
        null=True,
        blank=True,
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кем проверено",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    fixed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кем зафиксировано",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # Заявка, созданная через WFM: автор — пользователь WFM, а не Django-user,
    # поэтому FK невозможен. Храним снимок (Mongo ObjectId + имя на момент
    # создания) — только для отображения/аудита, бизнес-логика их не читает.
    wfm_created_by_id = models.CharField(
        verbose_name="Кем создана (WFM, id)",
        max_length=64,
        null=True,
        blank=True,
    )
    wfm_created_by_name = models.CharField(
        verbose_name="Кем создана (WFM, ФИО)",
        max_length=255,
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        verbose_name="Создана",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        verbose_name="Обновлена",
        auto_now=True,
    )

    correlation_id = models.CharField(
        verbose_name="Correlation ID (WFM sync)",
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
    )

    class Meta:
        abstract = True


class Compensation(BaseWorkRequest):
    """
    Заявка на компенсацию долга оператора.
    Вариант C: в день может быть несколько заявок (решает супервайзер).

    Источники (source):
      - "requested"  — обычная: оператор подал заявку заранее, потом отработает.
      - "retroactive"— ретроактивная: оператор уже отработал, потом подал заявку.
      - "system"     — создана системой автоматически.
      - "manual"     — создана супервайзером вручную.
    """
    SOURCE_CHOICES = [
        ("requested", "Запрошена заранее"),
        ("retroactive", "Ретроактивная (по факту)"),
        ("system", "Системная"),
        ("manual", "Вручную (супервайзером)"),
    ]

    type_rule = models.ForeignKey(
        "RequestTypeRule",
        verbose_name="Тип заявки",
        on_delete=models.PROTECT,
        limit_choices_to={"category": "compensation"},
        related_name="compensations",
    )

    source = models.CharField(
        verbose_name="Источник",
        max_length=20,
        choices=SOURCE_CHOICES,
        default="requested",
        db_index=True,
    )

    planned_date = models.DateField(
        verbose_name="Планируемая дата",
        db_index=True,
        help_text="Дата отработки (для retroactive — дата, когда оператор уже отработал)",
    )

    requested_duration = models.DurationField(
        verbose_name="Запрошенная длительность",
    )
    verified_duration = models.DurationField(
        verbose_name="Подтверждённая длительность",
        null=True,
        blank=True,
        help_text="Фактически засчитанное время после проверки",
    )
    remaining_debt = models.DurationField(
        verbose_name="Остаток долга",
        null=True,
        blank=True,
        help_text="Невыполненная часть (в статусе partial)",
    )

    deducted = models.BooleanField(
        verbose_name="Списано",
        default=False,
        help_text="Списано ли из WorkDebt.current_debt",
    )

    debts_snapshot = models.JSONField(
        verbose_name="Снимок долгов",
        default=list,
        blank=True,
        help_text="Снимок долгов на момент создания заявки",
    )

    # Поля для ретроактивной проверки
    claim_metadata = models.JSONField(
        verbose_name="Метаданные заявки",
        default=dict,
        blank=True,
        help_text=(
            "Дополнительный контекст для retroactive: свидетели, причина, "
            "почему оператор отработал без предварительной заявки и т.п."
        ),
    )
    auto_check_result = models.JSONField(
        verbose_name="Результат авто-проверки",
        default=dict,
        blank=True,
        help_text=(
            "Результат автоматической проверки: fact_full, fact_lock, "
            "shift_norm, available_overtime, computed_credit"
        ),
    )
    auto_check_at = models.DateTimeField(
        verbose_name="Дата авто-проверки",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "Компенсация"
        verbose_name_plural = "Компенсации"
        indexes = [
            models.Index(fields=["operator", "planned_date"]),
            models.Index(fields=["status"]),
            models.Index(fields=["cycle", "status"]),
            models.Index(fields=["planned_date", "status"]),
            models.Index(fields=["deducted"]),
            models.Index(fields=["source"]),
            models.Index(fields=["source", "status"]),
        ]
        constraints = [
            # WFM apply-request bitta correlation_id ni bir arizadagi HAR operatorga
            # yozadi. Yakka unique to'qnashardi; (correlation_id, operator) juftligi
            # bir operatorga bir correlation_id ni bir marta kafolatlaydi.
            # MySQL bir nechta NULL correlation_id ga ruxsat beradi (qo'lda yaratilgan).
            models.UniqueConstraint(
                fields=["correlation_id", "operator"],
                name="uq_compensation_corr_operator",
            ),
        ]
        ordering = ["-planned_date", "-created_at"]

    def clean(self):
        super().clean()

        if not self.type_rule:
            return
        rule = self.type_rule

        # Прошедшая дата разрешена только если allows_past_date=True
        if self.planned_date and self._state.adding:
            if self.planned_date < date.today() and not rule.allows_past_date:
                raise ValidationError({
                    "planned_date": (
                        f"Для типа «{rule.display_name}» подача заявок "
                        "на прошедшую дату запрещена. "
                        "Используйте ретроактивную проверку."
                    ),
                })

        # ЕДИНАЯ: любая заявка — только в пределах активного цикла.
        if self.planned_date and self._state.adding:
            cyc = Cycle.get_active()
            if cyc and not cyc.contains(self.planned_date):
                raise ValidationError({
                    "planned_date": (
                        f"Дата вне активного цикла ({cyc.start_date}–{cyc.end_date})."
                    ),
                })

        # Ограничения длительности
        if rule.min_duration and self.requested_duration < rule.min_duration:
            raise ValidationError({
                "requested_duration": (
                    f"Для этого типа минимальная длительность: {rule.min_duration}"
                ),
            })
        if rule.max_duration and self.requested_duration > rule.max_duration:
            raise ValidationError({
                "requested_duration": (
                    f"Для этого типа максимальная длительность: {rule.max_duration}"
                ),
            })

        # Ретроактив — только в активном цикле
        if self.source == "retroactive" and self.planned_date:
            cycle = Cycle.get_active()
            if cycle and not cycle.contains(self.planned_date):
                raise ValidationError({
                    "planned_date": (
                        f"Ретроактивная проверка доступна только в активном цикле "
                        f"({cycle.start_date}–{cycle.end_date}). "
                        "Прошедшие циклы хранятся в архиве."
                    ),
                })

        # nb_compensation: 9:00 yoki 12:00 + dam olish kuni + qarz mavjudligi
        if rule.code == "nb_compensation":
            self._validate_nb_compensation()

    def _validate_nb_compensation(self):
        """
        NB-kompensatsiya uchun maxsus qoidalar:
          1. requested_duration faqat 9:00 yoki 12:00 bo'lishi mumkin
          2. planned_date dam olish kuni bo'lishi shart
          3. Operatorda kamida shu qadar qarz bo'lishi kerak
        """
        from datetime import timedelta

        # 1. Davomiylik 9 yoki 12 soat
        allowed = {timedelta(hours=9), timedelta(hours=12)}
        if self.requested_duration not in allowed:
            raise ValidationError({
                "requested_duration": (
                    "NB-компенсация может быть только на 9:00:00 или 12:00:00 "
                    "(соответствует полной смене 9ч или 12ч)."
                ),
            })

        # 2. planned_date dam olish kuni
        if self.planned_date and self.operator_id:
            schedule = OperatorScheduleDay.objects.filter(
                operator_id=self.operator_id, day=self.planned_date,
            ).select_related("shift").first()

            if schedule and not schedule.is_day_off and schedule.shift:
                raise ValidationError({
                    "planned_date": (
                        f"На {self.planned_date} у оператора назначена смена "
                        f"{schedule.shift.code}. NB-компенсацию можно делать "
                        "только в выходной день."
                    ),
                })

        # 3. Operatorda yetarli qarz bormi (joriy tsikldagi WorkDebt)
        if self.operator_id and self._state.adding:
            cycle = Cycle.get_active()
            if cycle:
                wd = WorkDebt.objects.filter(
                    operator_id=self.operator_id, cycle=cycle,
                ).first()
                current_debt = wd.current_debt if wd else timedelta(0)
                if current_debt < self.requested_duration:
                    raise ValidationError({
                        "requested_duration": (
                            f"Недостаточно долга для NB-компенсации. "
                            f"Текущий долг оператора: {current_debt}, "
                            f"запрошено: {self.requested_duration}."
                        ),
                    })

    def __str__(self):
        type_code = self.type_rule.code if self.type_rule else "?"
        return (
            f"Комп[{self.get_source_display()}/{type_code}] "
            f"{self.operator} {self.planned_date} {self.requested_duration} "
            f"[{self.get_status_display()}]"
        )


class CompensationDebtLink(models.Model):
    """
    Связь Compensation ↔ WorkDebtDetail (through-модель).
    Со снимком — если запись долга удалена, данные сохраняются.
    """
    compensation = models.ForeignKey(
        "Compensation",
        verbose_name="Компенсация",
        on_delete=models.CASCADE,
        related_name="debt_links",
    )
    debt_detail = models.ForeignKey(
        "WorkDebtDetail",
        verbose_name="Запись долга",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="compensation_links",
        help_text="Если NULL — запись долга удалена, читайте snapshot",
    )

    snapshot = models.JSONField(
        verbose_name="Снимок долга",
        default=dict,
        help_text="Полная копия записи долга на момент создания связи",
    )

    applied = models.BooleanField(
        verbose_name="Применено",
        default=False,
        db_index=True,
        help_text="Списан ли долг по этой связи",
    )
    applied_amount = models.DurationField(
        verbose_name="Списанная сумма",
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        verbose_name="Создана",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        verbose_name="Обновлена",
        auto_now=True,
    )

    class Meta:
        verbose_name = "Связь компенсации с долгом"
        verbose_name_plural = "Связи компенсаций с долгами"
        indexes = [
            models.Index(fields=["compensation"]),
            models.Index(fields=["debt_detail"]),
            models.Index(fields=["applied"]),
        ]
        # MySQL не поддерживает conditional unique constraints,
        # поэтому проверка дубликатов делается на уровне сервиса
        # (compensation_verifier._apply_debt_links фильтрует applied=False).

    def __str__(self):
        state = "применено" if self.applied else "ожидает"
        return f"Comp#{self.compensation_id} ↔ Debt#{self.debt_detail_id} [{state}]"


class CompensationDay(models.Model):
    """
    Один день отработки в рамках заявки Compensation (тип «otrabotka»).

    Заявка отработки может охватывать несколько дней; общая
    requested_duration делится по дням (поле allocated_duration).
    Каждый день проверяется отдельно (Фаза 2: расчёт по факту из API),
    результат пишется в status/credited_duration/fact_*.
    """
    STATUS_CHOICES = [
        ("pending", "В ожидании"),
        ("approved", "Подтверждено"),
        ("partial", "Частично"),
        ("declined", "Отклонено"),
    ]

    compensation = models.ForeignKey(
        "Compensation",
        verbose_name="Заявка",
        on_delete=models.CASCADE,
        related_name="days",
    )
    day = models.DateField(
        verbose_name="День отработки",
        db_index=True,
    )
    allocated_duration = models.DurationField(
        verbose_name="Норма отработки на день",
        help_text="Доля requested_duration, распределённая на этот день",
    )

    status = models.CharField(
        verbose_name="Статус дня",
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
        db_index=True,
    )
    credited_duration = models.DurationField(
        verbose_name="Засчитано за день",
        null=True,
        blank=True,
        help_text="Фактически засчитанное время после проверки (Фаза 2)",
    )

    fact_full = models.DurationField(
        verbose_name="Факт (работа)",
        null=True,
        blank=True,
    )
    fact_lock = models.DurationField(
        verbose_name="Факт (перерыв)",
        null=True,
        blank=True,
    )

    verified_at = models.DateTimeField(
        verbose_name="Проверено",
        null=True,
        blank=True,
    )
    note = models.TextField(
        verbose_name="Примечание",
        blank=True,
        null=True,
    )

    created_at = models.DateTimeField(
        verbose_name="Создан",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        verbose_name="Обновлён",
        auto_now=True,
    )

    class Meta:
        verbose_name = "День отработки"
        verbose_name_plural = "Дни отработки"
        unique_together = ("compensation", "day")
        ordering = ["day"]
        indexes = [
            models.Index(fields=["day"]),
            models.Index(fields=["status"]),
            models.Index(fields=["compensation", "day"]),
        ]

    def __str__(self):
        return (
            f"Comp#{self.compensation_id} — {self.day} "
            f"[{self.allocated_duration}] {self.get_status_display()}"
        )


class Transfer(BaseWorkRequest):
    """
    Перенос смены / отгул / больничный / отпуск / учёба и т.п.
    Типы управляются через RequestTypeRule.
    """
    type_rule = models.ForeignKey(
        "RequestTypeRule",
        verbose_name="Тип заявки",
        on_delete=models.PROTECT,
        limit_choices_to={"category": "transfer"},
        related_name="transfers",
    )

    date_from = models.DateField(
        verbose_name="Дата начала",
        null=True,
        blank=True,
        db_index=True,
    )
    date_to = models.DateField(
        verbose_name="Дата окончания",
        null=True,
        blank=True,
        db_index=True,
    )
    hour_from = models.TimeField(
        verbose_name="Время начала",
        null=True,
        blank=True,
    )
    hour_to = models.TimeField(
        verbose_name="Время окончания",
        null=True,
        blank=True,
    )

    requested_duration = models.DurationField(
        verbose_name="Запрошенная длительность",
        null=True,
        blank=True,
    )
    verified_duration = models.DurationField(
        verbose_name="Подтверждённая длительность",
        null=True,
        blank=True,
    )
    remaining_debt = models.DurationField(
        verbose_name="Остаток долга",
        null=True,
        blank=True,
    )

    # Для isklyuchenie/obuchenie: как длительность распределяется при расчёте
    # отработки — часть уменьшает рабочую норму (wh), часть увеличивает
    # допустимый перерыв (lock). Функция отработки учитывает оба и суммирует.
    wh_part = models.DurationField(
        verbose_name="Доля в рабочие часы (wh)",
        null=True,
        blank=True,
        help_text="isklyuchenie/obuchenie: сколько уменьшает рабочую норму",
    )
    lock_part = models.DurationField(
        verbose_name="Доля в перерыв (lock)",
        null=True,
        blank=True,
        help_text="isklyuchenie/obuchenie: сколько добавляется к допустимому перерыву",
    )

    # Подтип заявки — ссылка на каталог RequestSubtypeRule (напр. для
    # «Прочие льготы»: Отгул / Короткий день). Параллельно type_rule.
    subtype_rule = models.ForeignKey(
        "RequestSubtypeRule",
        verbose_name="Подтип",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transfers",
    )

    # Только для otprashivanie: связанный план отработки (Compensation типа
    # otrabotka), которым оператор отрабатывает отпрошенные часы. Долги
    # (WDD source=time_off) за дни отпрашивания привязываются к этому плану.
    repayment_compensation = models.OneToOneField(
        "Compensation",
        verbose_name="План отработки (для отпрашивания)",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="otprashivanie_source",
    )

    class Meta:
        verbose_name = "Заявка (перенос/отгул)"
        verbose_name_plural = "Заявки (переносы/отгулы)"
        indexes = [
            models.Index(fields=["operator", "date_from"]),
            models.Index(fields=["operator", "date_to"]),
            models.Index(fields=["date_from", "date_to"]),
            models.Index(fields=["status"]),
            models.Index(fields=["cycle", "status"]),
            models.Index(fields=["type_rule", "status"]),
        ]
        constraints = [
            # WFM apply-request bitta correlation_id ni bir arizadagi HAR operatorga
            # yozadi. Yakka unique to'qnashardi; (correlation_id, operator) juftligi
            # bir operatorga bir correlation_id ni bir marta kafolatlaydi.
            # MySQL bir nechta NULL correlation_id ga ruxsat beradi (qo'lda yaratilgan).
            models.UniqueConstraint(
                fields=["correlation_id", "operator"],
                name="uq_transfer_corr_operator",
            ),
        ]
        ordering = ["-date_from", "-created_at"]

    def clean(self):
        super().clean()
        if not self.type_rule:
            return
        rule = self.type_rule

        if rule.requires_date_from and not self.date_from:
            raise ValidationError({
                "date_from": f"Для «{rule.display_name}» требуется дата начала"
            })
        if rule.requires_date_to and not self.date_to:
            raise ValidationError({
                "date_to": f"Для «{rule.display_name}» требуется дата окончания"
            })
        if rule.requires_hour_range and not (self.hour_from and self.hour_to):
            raise ValidationError(
                "Требуется диапазон часов (hour_from и hour_to)"
            )
        if rule.requires_duration and not self.requested_duration:
            raise ValidationError({
                "requested_duration": "Требуется длительность"
            })

        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValidationError(
                "Дата начала не может быть позже даты окончания"
            )

        # ЕДИНАЯ: transfer sanalari — aktiv sikl ichida + past emas (transferda retro yo'q).
        if self.date_from and self._state.adding:
            from .services.cycle import check_request_days
            span, d = [], self.date_from
            end = self.date_to or self.date_from
            while d <= end:
                span.append(d)
                d += timedelta(days=1)
            bad = check_request_days(span, allow_past=False)
            if bad:
                b = bad[0]
                if b["code"] == "out_of_cycle":
                    raise ValidationError({"date_from": (
                        f"Дата {b['day']} вне активного цикла ({b['cycle']}).")})
                raise ValidationError({"date_from": (
                    f"Заявку на прошедшую дату ({b['day']}) подать нельзя.")})

        if rule.min_duration and self.requested_duration and self.requested_duration < rule.min_duration:
            raise ValidationError({
                "requested_duration": f"Минимум: {rule.min_duration}"
            })
        if rule.max_duration and self.requested_duration and self.requested_duration > rule.max_duration:
            raise ValidationError({
                "requested_duration": f"Максимум: {rule.max_duration}"
            })

        # forbidden_on_day_off validatsiyasi — date_from yoki date_to
        # operatorning dam kuni bo'lsa, ariza yaratilmaydi
        if rule.forbidden_on_day_off and self.operator_id and self.date_from and self.date_to:
            forbidden_dates = []
            d = self.date_from
            while d <= self.date_to:
                schedule = OperatorScheduleDay.objects.filter(
                    operator_id=self.operator_id, day=d,
                ).select_related("shift").first()
                # Dam kun: schedule yo'q YOKI is_day_off=True YOKI shift yo'q
                if not schedule or schedule.is_day_off or not schedule.shift_id:
                    forbidden_dates.append(str(d))
                d += timedelta(days=1)
            if forbidden_dates:
                raise ValidationError({
                    "date_from": (
                        f"«{rule.display_name}» ariza turi dam kunlarida "
                        f"ruxsat etilmaydi. Dam kunlari: {', '.join(forbidden_dates)}"
                    )
                })

    def __str__(self):
        type_code = self.type_rule.code if self.type_rule else "?"
        return (
            f"Заявка[{type_code}] {self.operator} "
            f"{self.date_from}→{self.date_to} [{self.get_status_display()}]"
        )


class BenefitDay(models.Model):
    """
    Один день в рамках заявки-льготы (Transfer benefit-типы: Исключение,
    Обучение, Льготы, Хоз.работы). ОДНА заявка может покрывать несколько
    (в т.ч. непоследовательных) дней; по каждому дню — своё освобождение.

    duration=NULL → весь день освобождён (норма wh не учитывается).
    duration>0 → частичное (норма уменьшается на duration; окно hour_from–hour_to).
    debt_calculator списывает долг по этим записям (а не по диапазону дат),
    поэтому пропуски между днями НЕ освобождаются.
    """
    transfer = models.ForeignKey(
        "Transfer",
        verbose_name="Заявка-льгота",
        on_delete=models.CASCADE,
        related_name="benefit_days",
    )
    day = models.DateField(verbose_name="День", db_index=True)
    hour_from = models.TimeField(verbose_name="С (время)", null=True, blank=True)
    hour_to = models.TimeField(verbose_name="По (время)", null=True, blank=True)
    duration = models.DurationField(
        verbose_name="Длительность за день",
        null=True, blank=True,
        help_text="NULL = весь день; иначе уменьшение нормы на эту длительность",
    )

    created_at = models.DateTimeField(verbose_name="Создан", auto_now_add=True)
    updated_at = models.DateTimeField(verbose_name="Обновлён", auto_now=True)

    class Meta:
        verbose_name = "День льготы"
        verbose_name_plural = "Дни льготы"
        unique_together = ("transfer", "day")
        ordering = ["day"]
        indexes = [models.Index(fields=["day"])]

    def __str__(self):
        d = "весь день" if self.duration is None else str(self.duration)
        return f"Tr#{self.transfer_id} — {self.day} [{d}]"


class UchebaDay(models.Model):
    """
    Один день «учёбы в рабочее время» (Transfer, тип «ucheba_rabochee»).

    Учёба — окно в середине рабочего дня (напр. 12:00–18:00): оператор уходит
    учиться и ВОЗВРАЩАЕТСЯ доработать. Норма дня НЕ уменьшается — оператор
    обязан покрыть полную дневную норму, работая до/после учёбы (в т.ч. поздно,
    в следующие сутки). Окно ухода определяется по ФАКТУ из API (часы с
    full=0). Проверка по факту в расширенном окне (до остановки работы).
    Долг (WorkDebtDetail source="ucheba") создаётся, если норма не покрыта.
    Отработка может идти параллельно (доработка сверх нормы — отдельно).
    """
    STATUS_CHOICES = [
        ("pending", "В ожидании"),
        ("approved", "Подтверждено"),
        ("partial", "Частично"),
        ("declined", "Отклонено"),
    ]
    transfer = models.ForeignKey(
        "Transfer", verbose_name="Заявка учёбы",
        on_delete=models.CASCADE, related_name="ucheba_days",
    )
    day = models.DateField(verbose_name="День", db_index=True)
    hour_from = models.TimeField(verbose_name="С (план)", null=True, blank=True)
    hour_to = models.TimeField(verbose_name="По (план)", null=True, blank=True)
    duration = models.DurationField(
        verbose_name="Длительность учёбы (план)", null=True, blank=True)

    fact_full = models.DurationField(verbose_name="Факт (полный)", null=True, blank=True)
    fact_lock = models.DurationField(verbose_name="Факт (перерыв)", null=True, blank=True)
    debt = models.DurationField(verbose_name="Долг дня", null=True, blank=True)
    status = models.CharField(
        verbose_name="Статус", max_length=20,
        choices=STATUS_CHOICES, default="pending", db_index=True)
    verified_at = models.DateTimeField(verbose_name="Проверено", null=True, blank=True)
    note = models.TextField(verbose_name="Примечание", null=True, blank=True)

    created_at = models.DateTimeField(verbose_name="Создан", auto_now_add=True)
    updated_at = models.DateTimeField(verbose_name="Обновлён", auto_now=True)

    class Meta:
        verbose_name = "День учёбы"
        verbose_name_plural = "Дни учёбы"
        unique_together = ("transfer", "day")
        ordering = ["day"]
        indexes = [models.Index(fields=["day"]), models.Index(fields=["status"])]

    def __str__(self):
        return f"Tr#{self.transfer_id} — {self.day} [учёба {self.duration}]"


class OtprashivanieDay(models.Model):
    """
    Один день отпрашивания в рамках заявки Transfer (тип «otprashivanie»).

    Заявка может охватывать несколько дней; на каждый день — своё окно времени
    (hour_from–hour_to) и длительность (отпрошенные часы за этот день).
    За каждый такой день в дневном пайплайне создаётся долг
    (WorkDebtDetail source="time_off"), привязанный к плану отработки
    (Transfer.repayment_compensation).
    """
    transfer = models.ForeignKey(
        "Transfer",
        verbose_name="Заявка отпрашивания",
        on_delete=models.CASCADE,
        related_name="otprashivanie_days",
    )
    day = models.DateField(verbose_name="День отпрашивания", db_index=True)
    hour_from = models.TimeField(verbose_name="С (время)")
    hour_to = models.TimeField(verbose_name="По (время)")
    duration = models.DurationField(verbose_name="Длительность за день")

    created_at = models.DateTimeField(verbose_name="Создан", auto_now_add=True)
    updated_at = models.DateTimeField(verbose_name="Обновлён", auto_now=True)

    class Meta:
        verbose_name = "День отпрашивания"
        verbose_name_plural = "Дни отпрашивания"
        unique_together = ("transfer", "day")
        ordering = ["day"]
        indexes = [models.Index(fields=["day"])]

    def __str__(self):
        return f"Tr#{self.transfer_id} — {self.day} [{self.duration}]"


# =============================================================================
# СЛОЙ 3: ManualAdjustment, AutomationOverride, Note
# =============================================================================

class ManualAdjustment(models.Model):
    TARGET_CHOICES = [
        ("debt_detail", "Запись долга (дневная)"),
        ("debt_total", "Общий долг (WorkDebt)"),
        ("compensation", "Компенсация"),
        ("transfer", "Перенос/Отгул"),
        ("log_daily", "Ежедневный лог"),
        ("schedule_day", "День расписания"),
    ]
    REASON_CHOICES = [
        ("emergency", "Чрезвычайная ситуация"),
        ("system_error", "Системная ошибка"),
        ("management_decision", "Решение руководства"),
        ("data_correction", "Исправление данных"),
        ("policy_exception", "Исключение из правил"),
        ("manual_verification", "Ручная проверка"),
        ("other", "Другое"),
    ]

    target_type = models.CharField(
        verbose_name="Тип объекта",
        max_length=30,
        choices=TARGET_CHOICES,
        db_index=True,
    )
    target_id = models.PositiveIntegerField(
        verbose_name="ID объекта",
        db_index=True,
    )

    operator = models.ForeignKey(
        "Operator",
        verbose_name="Оператор",
        on_delete=models.PROTECT,
        related_name="adjustments",
        null=True,
        blank=True,
        help_text="Денормализация — для быстрого поиска по оператору",
    )

    field_name = models.CharField(
        verbose_name="Поле",
        max_length=100,
    )
    old_value = models.JSONField(
        verbose_name="Старое значение",
        null=True,
        blank=True,
    )
    new_value = models.JSONField(
        verbose_name="Новое значение",
    )

    reason_code = models.CharField(
        verbose_name="Код причины",
        max_length=50,
        choices=REASON_CHOICES,
    )
    reason_text = models.TextField(
        verbose_name="Описание причины",
        help_text="Подробное описание — обязательно",
    )

    adjusted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кем правлено",
        on_delete=models.PROTECT,
        related_name="adjustments_made",
    )
    adjusted_at = models.DateTimeField(
        verbose_name="Когда правлено",
        auto_now_add=True,
    )

    requires_approval = models.BooleanField(
        verbose_name="Требует подтверждения",
        default=False,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кем подтверждено",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="adjustments_approved",
    )
    approved_at = models.DateTimeField(
        verbose_name="Когда подтверждено",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = "Ручная правка"
        verbose_name_plural = "Ручные правки"
        indexes = [
            models.Index(fields=["target_type", "target_id"]),
            models.Index(fields=["operator"]),
            models.Index(fields=["adjusted_at"]),
            models.Index(fields=["adjusted_by"]),
            models.Index(fields=["reason_code"]),
        ]
        ordering = ["-adjusted_at"]

    def __str__(self):
        return (
            f"{self.get_target_type_display()}#{self.target_id}.{self.field_name} "
            f"от {self.adjusted_by} ({self.adjusted_at:%Y-%m-%d %H:%M})"
        )


class AutomationOverride(models.Model):
    """
    Отключение автоматической логики для конкретного оператора.
    Пример: не считать долг для больного оператора.
    """
    OVERRIDE_TYPE_CHOICES = [
        ("skip_debt_calc", "Не считать долг"),
        ("skip_log_load", "Не загружать лог"),
        ("manual_verification_only", "Только ручная проверка"),
        ("freeze_total_debt", "Заморозить общий долг"),
        ("skip_compensation_check", "Не проверять компенсации автоматически"),
        ("skip_transfer_check", "Не проверять переносы автоматически"),
    ]

    operator = models.ForeignKey(
        "Operator",
        verbose_name="Оператор",
        on_delete=models.CASCADE,
        related_name="automation_overrides",
    )
    override_type = models.CharField(
        verbose_name="Тип переопределения",
        max_length=50,
        choices=OVERRIDE_TYPE_CHOICES,
        db_index=True,
    )

    valid_from = models.DateField(
        verbose_name="Действует с",
    )
    valid_to = models.DateField(
        verbose_name="Действует до",
        null=True,
        blank=True,
        help_text="NULL = бессрочно",
    )

    reason = models.TextField(
        verbose_name="Причина",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кем создано",
        on_delete=models.PROTECT,
        related_name="overrides_created",
    )
    created_at = models.DateTimeField(
        verbose_name="Когда создано",
        auto_now_add=True,
    )

    is_active = models.BooleanField(
        verbose_name="Активно",
        default=True,
        db_index=True,
    )
    deactivated_at = models.DateTimeField(
        verbose_name="Когда деактивировано",
        null=True,
        blank=True,
    )
    deactivated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кем деактивировано",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="overrides_deactivated",
    )

    class Meta:
        verbose_name = "Переопределение автоматики"
        verbose_name_plural = "Переопределения автоматики"
        indexes = [
            models.Index(fields=["operator", "is_active"]),
            models.Index(fields=["override_type", "is_active"]),
            models.Index(fields=["valid_from", "valid_to"]),
        ]

    def is_active_on(self, day: date) -> bool:
        if not self.is_active:
            return False
        if day < self.valid_from:
            return False
        if self.valid_to and day > self.valid_to:
            return False
        return True

    def __str__(self):
        end = self.valid_to or "∞"
        return (
            f"{self.operator} — {self.get_override_type_display()} "
            f"({self.valid_from}–{end})"
        )


class Note(models.Model):
    """
    Свободный текстовый комментарий к любой записи.
    Видимость можно ограничить через visibility.
    """
    TARGET_CHOICES = [
        ("operator", "Оператор"),
        ("debt_detail", "Запись долга"),
        ("compensation", "Компенсация"),
        ("transfer", "Перенос/Отгул"),
        ("log_daily", "Ежедневный лог"),
        ("schedule_day", "День расписания"),
        ("cycle", "Цикл"),
    ]
    VISIBILITY_CHOICES = [
        ("public", "Все видят"),
        ("operator", "Сам оператор"),
        ("supervisor", "Супервайзер и выше"),
        ("manager_only", "Только менеджер"),
    ]

    target_type = models.CharField(
        verbose_name="Тип объекта",
        max_length=30,
        choices=TARGET_CHOICES,
        db_index=True,
    )
    target_id = models.PositiveIntegerField(
        verbose_name="ID объекта",
        db_index=True,
    )

    text = models.TextField(
        verbose_name="Текст",
    )
    visibility = models.CharField(
        verbose_name="Видимость",
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default="supervisor",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Автор",
        on_delete=models.PROTECT,
    )
    created_at = models.DateTimeField(
        verbose_name="Создан",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        verbose_name="Обновлён",
        auto_now=True,
    )

    class Meta:
        verbose_name = "Примечание"
        verbose_name_plural = "Примечания"
        indexes = [
            models.Index(fields=["target_type", "target_id"]),
            models.Index(fields=["created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"Примечание к {self.get_target_type_display()}#{self.target_id} "
            f"от {self.created_by_id}"
        )


# =============================================================================
# СЛОЙ 4: EventLog
# =============================================================================

class EventLog(models.Model):
    """
    Журнал всех значимых событий системы.
    Используется для отладки, аудита и расследований.
    """
    EVENT_TYPE_CHOICES = [
        ("log_loaded", "Лог загружен"),
        ("log_load_failed", "Ошибка загрузки лога"),
        ("debt_calculated", "Долг рассчитан"),
        ("compensation_created", "Компенсация создана"),
        ("compensation_verified", "Компенсация проверена"),
        ("transfer_created", "Заявка создана"),
        ("transfer_completed", "Заявка завершена"),
        ("cycle_opened", "Цикл открыт"),
        ("cycle_closing", "Цикл закрывается"),
        ("cycle_closed", "Цикл закрыт"),
        ("archived", "Запись архивирована"),
        ("manual_adjustment", "Ручная правка"),
        ("automation_override", "Автоматика отключена"),
        ("api_error", "Ошибка API"),
        ("sheets_synced", "Sheets синхронизированы"),
        ("schedule_changed", "Расписание изменено"),
        ("night_pipeline_run", "Ночной конвейер запущен"),
        ("retroactive_check", "Ретроактивная проверка"),
    ]
    LEVEL_CHOICES = [
        ("debug", "DEBUG"),
        ("info", "INFO"),
        ("warning", "WARNING"),
        ("error", "ERROR"),
        ("critical", "CRITICAL"),
    ]

    event_type = models.CharField(
        verbose_name="Тип события",
        max_length=50,
        choices=EVENT_TYPE_CHOICES,
        db_index=True,
    )
    level = models.CharField(
        verbose_name="Уровень",
        max_length=20,
        choices=LEVEL_CHOICES,
        default="info",
        db_index=True,
    )

    operator = models.ForeignKey(
        "Operator",
        verbose_name="Оператор",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
    )
    cycle = models.ForeignKey(
        "Cycle",
        verbose_name="Цикл",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    target_type = models.CharField(
        verbose_name="Тип объекта",
        max_length=30,
        blank=True,
        null=True,
    )
    target_id = models.PositiveIntegerField(
        verbose_name="ID объекта",
        null=True,
        blank=True,
    )

    message = models.TextField(
        verbose_name="Сообщение",
        blank=True,
    )
    payload = models.JSONField(
        verbose_name="Данные",
        default=dict,
        blank=True,
    )

    timestamp = models.DateTimeField(
        verbose_name="Время",
        auto_now_add=True,
        db_index=True,
    )
    triggered_by = models.ForeignKey(
        User,
        verbose_name="Триггер",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="triggered_events",
    )

    class Meta:
        verbose_name = "Событие"
        verbose_name_plural = "События"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["event_type", "level"]),
            models.Index(fields=["operator", "timestamp"]),
            models.Index(fields=["cycle", "level"]),
        ]

    def __str__(self):
        return f"[{self.level}] {self.event_type} @ {self.timestamp:%Y-%m-%d %H:%M}"


class RequestActionLog(models.Model):
    """
    Аудит-лог действий над заявками (compensation/transfer) через WFM API.

    Создаётся и хранится в WFM (при каждом POST/PATCH/GET/DELETE), затем
    push'ится в Django. Идемпотентность — по wfm_id (Mongo _id лога).

    payload зависит от метода:
      POST   — тело создания (что создано)
      PATCH  — изменённые поля {before, after}
      GET(list)   — query/фильтры
      GET(detail) — {id}
      DELETE — полный снимок удалённой заявки (данные не теряются)
    """
    METHOD_CHOICES = [
        ("POST", "POST"), ("PATCH", "PATCH"), ("GET", "GET"), ("DELETE", "DELETE"),
    ]
    ENTITY_CHOICES = [("compensation", "compensation"), ("transfer", "transfer")]

    wfm_id = models.CharField(
        verbose_name="WFM лог _id", max_length=64, unique=True,
        help_text="Mongo _id лога в WFM — ключ идемпотентности при push.",
    )
    user_id = models.CharField(verbose_name="WFM user _id", max_length=64, blank=True, default="")
    user_name = models.CharField(verbose_name="Пользователь", max_length=255, blank=True, default="")
    method = models.CharField(verbose_name="Метод", max_length=10, choices=METHOD_CHOICES)
    entity_type = models.CharField(
        verbose_name="Тип заявки", max_length=20, choices=ENTITY_CHOICES,
    )
    entity_external_id = models.BigIntegerField(
        verbose_name="external_id заявки", null=True, blank=True,
        help_text="external_id заявки; null для GET-списка.",
    )
    entity_wfm_id = models.CharField(
        verbose_name="ID заявки (WFM)", max_length=64, blank=True, default="",
        help_text="Mongo _id самой заявки в WFM (POST — из ответа, иначе — из пути).",
    )
    endpoint = models.CharField(verbose_name="Endpoint", max_length=500)
    payload = models.JSONField(verbose_name="Данные", default=dict, blank=True)
    status_code = models.PositiveSmallIntegerField(
        verbose_name="HTTP статус", null=True, blank=True,
    )
    success = models.BooleanField(verbose_name="Успех", default=True)
    created_at = models.DateTimeField(verbose_name="Дата/время")

    class Meta:
        verbose_name = "Лог действия над заявкой"
        verbose_name_plural = "Логи действий над заявками"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user_id"]),
            models.Index(fields=["entity_type", "entity_external_id"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["method"]),
        ]

    def __str__(self):
        who = self.user_name or self.user_id or "?"
        tgt = f"{self.entity_type} #{self.entity_external_id}" if self.entity_external_id else self.entity_type
        return f"{who} — {self.method} {tgt} [{self.status_code}]"
