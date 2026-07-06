"""
Вспомогательные функции.
"""

from datetime import timedelta
from typing import Optional

from django.db.models import Prefetch


def normalize_text(value) -> str:
    """Удаляет пробелы и приводит к нижнему регистру для сравнения ФИО."""
    if not value:
        return ""
    return (
        str(value)
        .replace(" ", "")
        .replace(" ", "")
        .strip()
        .lower()
    )


def normalize_fio_for_match(fio: str) -> str:
    """Нормализация ФИО для сравнения с Google Sheets.

    Все варианты апострофа/тутук белгиси УДАЛЯЮТСЯ (o'g'li / o`g`li / oʻgʻli /
    ogli — все становятся одинаковыми). Раньше нормализовались только фигурные
    кавычки, но Django хранил backtick (o`g`li), а Sheets — прямой ' (o'g'li),
    из-за чего ФИО не совпадали и график не загружался.
    """
    if not fio:
        return ""
    fio = str(fio).lower().strip()
    for ch in ("'", "`", "’", "‘", "ʻ", "ʼ", "´", "‛", "＇"):
        fio = fio.replace(ch, "")
    fio = " ".join(fio.split())
    return fio


def build_operator_full_name(operator) -> str:
    """Возвращает полное ФИО оператора."""
    return " ".join(
        part for part in [
            operator.surname,
            operator.name,
            operator.middle_name,
        ]
        if part
    )


def get_operator_by_fio_slug(operator_slug: str):
    """
    Находит оператора по slug ФИО (например: 'ivanov_ivan_ivanovich').
    """
    from hourly_locks.models import Operator, WorkDebtDetail

    operators = (
        Operator.objects
        .select_related("group")
        .prefetch_related(
            Prefetch(
                "debt_details",
                queryset=WorkDebtDetail.objects.order_by("-day", "-created_at"),
            )
        )
    )

    normalized_slug = normalize_text(operator_slug)

    for operator in operators:
        full_name = build_operator_full_name(operator)
        if normalize_text(full_name) == normalized_slug:
            return operator

    return None


def get_operator_group_name(operator) -> Optional[str]:
    """Возвращает название группы оператора (или None)."""
    group = getattr(operator, "group", None)
    if not group:
        return None
    return str(group.name) if group.name else str(group.id)


def format_duration(value: Optional[timedelta]) -> str:
    """Преобразует timedelta в строку HH:MM:SS."""
    if not value or value.total_seconds() <= 0:
        return "00:00:00"

    total_seconds = int(value.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_duration_with_days(value: Optional[timedelta]) -> str:
    """Преобразует timedelta в строку «N дней HH:MM:SS»."""
    if not value or value.total_seconds() <= 0:
        return "0 дн. 00:00:00"

    total_seconds = int(value.total_seconds())
    days = total_seconds // 86400
    rem = total_seconds - days * 86400

    hours = rem // 3600
    minutes = (rem % 3600) // 60
    seconds = rem % 60

    return f"{days} дн. {hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_hhmmss(value: str) -> timedelta:
    """Преобразует строку 'HH:MM:SS' в timedelta."""
    if not value:
        return timedelta(0)
    try:
        h, m, s = map(int, value.split(":"))
        return timedelta(hours=h, minutes=m, seconds=s)
    except (ValueError, AttributeError):
        return timedelta(0)


def get_system_policy(key: str, default=None):
    """
    Получить значение системной политики по ключу.
    Использует кэш.
    """
    from hourly_locks.models import SystemPolicy
    from django.core.cache import cache

    cache_key = f"system_policy:{key}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        policy = SystemPolicy.objects.get(key=key)
        value = policy.value
    except SystemPolicy.DoesNotExist:
        value = default

    cache.set(cache_key, value, timeout=300)
    return value


def invalidate_system_policy_cache(key: str):
    """Сбрасывает кэш для конкретной политики."""
    from django.core.cache import cache
    cache.delete(f"system_policy:{key}")