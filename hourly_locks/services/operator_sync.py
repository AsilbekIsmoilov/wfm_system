"""
Авто-создание операторов на основе данных внешнего API.

Когда API возвращает login_id, которого нет в БД — создаём Operator
с распарсенным ФИО, без группы, и пишем EventLog (warning),
чтобы супервайзер обратил внимание.

ВАЖНО:
  - Существующие операторы НЕ перезаписываются (в отличие от старого кода).
  - ФИО разбивается на surname/name/middle_name только при создании.
  - Можно отключить через SystemPolicy "auto_create_operators_from_api".
"""

import logging
from datetime import date
from difflib import SequenceMatcher
from typing import Set

from django.db import transaction

from hourly_locks.models import Operator
from hourly_locks.utils import get_system_policy, normalize_fio_for_match

from . import external_api
from .event_log import log_event

logger = logging.getLogger(__name__)

# Порог схожести нормализованных ФИО для отличия РЕАЛЬНОГО переназначения логина
# (другой человек, низкая схожесть) от орфографической вариации того же ФИО
# (высокая схожесть). Эмпирически: реальные переназначения ≤ 0.54, орфография
# ≥ 0.80 — берём порог 0.65 (широкий зазор). Ниже порога → rename, выше → пропуск.
REASSIGN_SIMILARITY_THRESHOLD = 0.65


def sync_operators_from_api(target_date: date, sample_hours: range = None) -> dict:
    """
    Просматривает API за указанный день и создаёт новых операторов,
    которых нет в БД.

    Args:
        target_date: дата для проверки
        sample_hours: какие часы запрашивать (по умолчанию 10-15 — рабочее время,
                      чтобы охватить все смены)

    Returns:
        dict со статистикой: {created: N, existing: N, total_unique: N}
    """
    enabled = get_system_policy("auto_create_operators_from_api", True)
    if not enabled:
        logger.info("[operator_sync] Отключено через SystemPolicy")
        return {"created": 0, "existing": 0, "skipped": "disabled"}

    if sample_hours is None:
        sample_hours = range(10, 16)  # 10:00-15:00 — большинство смен активны

    logger.info("[operator_sync] Сканирование API за %s, часы %s", target_date, list(sample_hours))

    # Собираем уникальные логины с ФИО
    unique_operators = {}  # login_id -> FullName

    for hour in sample_hours:
        rows = external_api.fetch_hour(target_date, hour)
        if not rows:
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            login = (row.get("login") or "").strip()
            fio = (row.get("FullName") or "").strip()
            if not login or not fio:
                continue
            # Первое появление имени запоминаем
            if login not in unique_operators:
                unique_operators[login] = fio

    if not unique_operators:
        logger.info("[operator_sync] В API нет операторов за %s", target_date)
        return {"created": 0, "existing": 0, "total_unique": 0}

    # Существующие операторы по login_id (как объекты — нужно для сверки ФИО).
    existing_by_login = {
        o.login_id: o
        for o in Operator.objects.filter(
            login_id__in=list(unique_operators.keys()))
    }
    new_logins = set(unique_operators.keys()) - set(existing_by_login.keys())

    created = 0
    reassigned = 0
    with transaction.atomic():
        # (0) ПЕРЕНАЗНАЧЕНИЕ ЛОГИНА: login_id уже есть, но в API — ДРУГОЙ человек
        #     (прежний уволен, номер выдали новому). login_id — переиспользуемый
        #     номер, поэтому обновляем запись ПОД ТЕКУЩЕГО владельца (rename in
        #     place): без дублей, без конфликта unique, без churn в WFM.
        #
        #     ВАЖНО: отличать РЕАЛЬНОЕ переназначение (другой человек) от
        #     орфографической разницы того же ФИО (Baxtiyorovna/Baxtiyarovna,
        #     a/o, x/h, k/q). Иначе rename на «API-написание» сломает совпадение
        #     с Google Sheets (график ищет по ФИО) → оператор останется без
        #     графика. Решаем по СХОЖЕСТИ нормализованных ФИО (SequenceMatcher):
        #     низкая схожесть = другой человек → rename; высокая = та же
        #     орфографическая вариация → НЕ трогаем (сохраняем совпадение с Sheets).
        for login_id, op in existing_by_login.items():
            api_fio = unique_operators[login_id]
            db_fio = f"{op.surname} {op.name} {op.middle_name or ''}".strip()
            n_api = normalize_fio_for_match(api_fio)
            n_db = normalize_fio_for_match(db_fio)
            if n_api == n_db:
                continue  # тот же человек — ничего не делаем
            similarity = SequenceMatcher(None, n_db, n_api).ratio()
            if similarity >= REASSIGN_SIMILARITY_THRESHOLD:
                # Орфографическая вариация того же ФИО — оставляем как есть,
                # чтобы не порвать совпадение с Sheets.
                logger.info(
                    "[operator_sync] Логин %s: орфографическая вариация ФИО "
                    "(схожесть %.2f), пропуск: «%s» ~ «%s»",
                    login_id, similarity, db_fio, api_fio,
                )
                continue
            surname, name, middle_name = _parse_fio(api_fio)
            op.surname, op.name, op.middle_name = surname, name, middle_name
            op.is_active = True
            op.save(update_fields=[
                "surname", "name", "middle_name", "is_active", "updated_at",
            ])
            reassigned += 1
            log_event(
                event_type="sheets_synced",
                level="warning",
                message=(
                    f"Логин переназначен: login_id={login_id} "
                    f"«{db_fio}» → «{api_fio}» (ФИО обновлено по API). "
                    f"Проверьте группу в админке."
                ),
                target_type="operator",
                payload={
                    "login_id": login_id,
                    "old_fio": db_fio,
                    "new_fio": api_fio,
                    "similarity": round(similarity, 2),
                    "reassigned_at": str(target_date),
                },
            )
            logger.warning(
                "[operator_sync] Логин %s переназначен (схожесть %.2f): %s -> %s",
                login_id, similarity, db_fio, api_fio,
            )

        # (1) Новые логины — создаём.
        for login_id in new_logins:
            fio = unique_operators[login_id]
            surname, name, middle_name = _parse_fio(fio)

            try:
                Operator.objects.create(
                    login_id=login_id,
                    surname=surname,
                    name=name,
                    middle_name=middle_name,
                    is_active=True,
                )
                created += 1

                log_event(
                    event_type="sheets_synced",  # Используем близкий тип
                    level="warning",
                    message=(
                        f"Авто-создан новый оператор из API: "
                        f"{fio} (login_id={login_id}). "
                        f"Назначьте группу через админку."
                    ),
                    target_type="operator",
                    payload={
                        "login_id": login_id,
                        "fio": fio,
                        "auto_created_at": str(target_date),
                    },
                )
                logger.warning(
                    "[operator_sync] Создан оператор: %s %s %s (login=%s)",
                    surname, name, middle_name or "", login_id,
                )

            except Exception as exc:
                logger.exception(
                    "[operator_sync] Ошибка создания оператора %s: %s",
                    fio, exc,
                )

    stats = {
        "created": created,
        "reassigned": reassigned,
        "existing": len(existing_by_login),
        "total_unique": len(unique_operators),
    }
    logger.info("[operator_sync] Завершено: %s", stats)
    return stats


def _parse_fio(full_name: str) -> tuple:
    """Разбирает ФИО: 'Иванов Иван Иванович' -> ('Иванов', 'Иван', 'Иванович')."""
    parts = full_name.strip().split(maxsplit=2)
    surname = parts[0] if len(parts) >= 1 else ""
    name = parts[1] if len(parts) >= 2 else ""
    middle_name = parts[2] if len(parts) >= 3 else None
    return surname, name, middle_name