"""
Единый шлюз ограничения по группам (deny-list).

Группа с `is_excluded=True` полностью выключается из обработки: для её
операторов НЕ считается долг, НЕ грузятся логи, нет посещаемости и нельзя
подать заявку. Это ЕДИНСТВЕННЫЙ источник правды — все подсистемы (attendance,
debt, pipeline, приём заявок и будущие фичи) спрашивают только здесь.

Использование:
    from .group_scope import (
        is_operator_excluded, exclude_operators, excluded_group_ids,
    )

    # queryset операторов (или связанных моделей) без исключённых групп:
    qs = exclude_operators(OperatorScheduleDay.objects.filter(day=d), path="operator")

    # проверка одного оператора (напр. при приёме заявки):
    if is_operator_excluded(operator):
        raise ...

Кэш: список исключённых групп меняется редко (админ-переключатель), поэтому
кэшируем на короткий TTL. cache сбрасывается сигналом при сохранении Group.
"""
from django.core.cache import cache

_CACHE_KEY = "excluded_group_ids_v1"
_CACHE_TTL = 300  # секунд


def excluded_group_ids() -> set:
    """ID всех групп с is_excluded=True (кэшируется). Пустое множество —
    ограничений нет (обычный режим)."""
    ids = cache.get(_CACHE_KEY)
    if ids is None:
        from ..models import Group
        ids = set(
            Group.objects.filter(is_excluded=True).values_list("id", flat=True)
        )
        cache.set(_CACHE_KEY, ids, _CACHE_TTL)
    return ids


def invalidate_cache() -> None:
    """Сбросить кэш (вызывается сигналом при сохранении/удалении Group)."""
    cache.delete(_CACHE_KEY)


def is_group_excluded(group_id) -> bool:
    return group_id is not None and group_id in excluded_group_ids()


def is_operator_excluded(operator) -> bool:
    """True — оператора нужно ПОЛНОСТЬЮ пропустить (его группа исключена).
    Оператор без группы НЕ исключён (обрабатывается как обычно)."""
    if operator is None:
        return False
    gid = getattr(operator, "group_id", None)
    return is_group_excluded(gid)


def exclude_operators(qs, path: str = ""):
    """Убрать из queryset записи операторов исключённых групп.

    path — путь до group_id от модели queryset:
      • "" (по умолчанию)  — сам Operator (поле group_id);
      • "operator"         — модель со связью operator (OperatorScheduleDay,
                             WorkLogDaily, Transfer, Compensation, ...).
    Если исключённых групп нет — queryset возвращается без изменений.
    """
    ids = excluded_group_ids()
    if not ids:
        return qs
    field = f"{path}__group_id" if path else "group_id"
    return qs.exclude(**{f"{field}__in": ids})


def allowed_operator_ids(operator_ids):
    """Из списка operator_id убрать тех, чья группа исключена."""
    ids = excluded_group_ids()
    if not ids:
        return list(operator_ids)
    from ..models import Operator
    blocked = set(
        Operator.objects.filter(
            id__in=operator_ids, group_id__in=ids,
        ).values_list("id", flat=True)
    )
    return [oid for oid in operator_ids if oid not in blocked]
