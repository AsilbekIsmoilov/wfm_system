"""
Синхронизация с Google Sheets:
  - sync_groups()    — обновляет принадлежность операторов к группам
  - sync_schedules() — обновляет расписание (OperatorScheduleDay) за текущий месяц
"""

import calendar
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import gspread
from django.conf import settings
from django.db import transaction
from google.oauth2.service_account import Credentials
from oauth2client.service_account import ServiceAccountCredentials

from hourly_locks.models import Group, Operator, OperatorScheduleDay
from hourly_locks.utils import (
    get_system_policy,
    normalize_fio_for_match,
)

from . import shift as shift_service
from .event_log import log_event

logger = logging.getLogger(__name__)


# =============================================================================
# Получение клиента Google Sheets
# =============================================================================

def _get_gspread_client(use_oauth2: bool = False):
    """
    Создаёт клиент gspread.
    Путь к creds.json берётся из settings.GOOGLE_CREDENTIAL_PATH.
    """
    credential_path = getattr(
        settings, "GOOGLE_CREDENTIAL_PATH", None,
    ) or get_system_policy("google_credential_path", "C:/Users/User/Documents/wc_system_v2/hour_by_project/creds.json")

    if use_oauth2:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        credentials = ServiceAccountCredentials.from_json_keyfile_name(
            credential_path, scope,
        )
        return gspread.authorize(credentials)

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(credential_path, scopes=scopes)
    return gspread.authorize(creds)


# =============================================================================
# Синхронизация групп
# =============================================================================

def sync_groups_from_sheets() -> dict:
    """
    Синхронизирует принадлежность операторов к группам из Google Sheets.
    Sheet ID и название листа — в SystemPolicy.
    """
    sheet_id = get_system_policy(
        "groups_sheet_id",
        "18snbRd84nZRqRa53ydSlzQxwbaY0h8DMTHCiF00OFpg",
    )
    sheet_name = get_system_policy(
        "groups_sheet_name",
        "IMPORT_255 PM OPERATOR TRADE",
    )

    logger.info("[sheets_sync] Синхронизация групп из Sheets...")

    try:
        client = _get_gspread_client(use_oauth2=True)
        sheet = client.open_by_key(sheet_id).worksheet(sheet_name)
        data = sheet.get_all_values()
    except Exception as exc:
        logger.exception("[sheets_sync] Ошибка чтения Sheets: %s", exc)
        log_event(
            event_type="sheets_synced",
            level="error",
            message=f"Ошибка чтения Sheets: {exc}",
        )
        return {"error": str(exc)}

    if not data or len(data) < 2:
        logger.warning("[sheets_sync] Таблица пуста")
        return {"updated": 0, "not_found": 0}

    operator_map = _build_operator_map()

    updated = 0
    not_found = []
    created_groups = set()

    with transaction.atomic():
        for row in data[1:]:
            try:
                fio = row[0].strip() if len(row) > 0 else ""
                group_name = row[2].strip() if len(row) > 2 else ""

                if not fio or not group_name:
                    continue

                norm_fio = normalize_fio_for_match(fio)
                operator = operator_map.get(norm_fio)

                if not operator:
                    not_found.append(fio)
                    continue

                if operator.role != "operator":
                    continue

                group, _ = Group.objects.get_or_create(name=group_name)

                if operator.group_id != group.id:
                    operator.group = group
                    operator.save(update_fields=["group", "updated_at"])
                    updated += 1
                    created_groups.add(group_name)

            except Exception as exc:
                logger.error("[sheets_sync] Ошибка в строке %s: %s", row, exc)

    log_event(
        event_type="sheets_synced",
        level="info",
        message=f"Синхронизация групп завершена. Обновлено: {updated}",
        payload={
            "updated": updated,
            "not_found_count": len(not_found),
            "groups": list(created_groups),
        },
    )

    logger.info(
        "[sheets_sync] Синхронизация групп: обновлено %d, не найдено %d",
        updated, len(not_found),
    )

    return {
        "updated": updated,
        "not_found": len(not_found),
        "groups": list(created_groups),
    }


def _build_operator_map() -> dict:
    """Возвращает {normalized_fio: Operator}."""
    mapping = {}
    for op in Operator.objects.all():
        fio = f"{op.surname} {op.name} {op.middle_name or ''}"
        norm = normalize_fio_for_match(fio)
        mapping[norm] = op
    return mapping


# =============================================================================
# Синхронизация расписания
# =============================================================================

def sync_schedules_from_sheets(
    target_month: Optional[date] = None,
) -> dict:
    """
    Синхронизирует расписание на текущий (или указанный) месяц.

    Каждый день записывается в OperatorScheduleDay.
    """
    if target_month is None:
        target_month = date.today()

    year = target_month.year
    month = target_month.month
    last_day = calendar.monthrange(year, month)[1]

    sheet_1_id = get_system_policy(
        "schedule_sheet_1_id",
        "1l8TF85YRE2bNo6VOcWlDEpBKRMGrwfdV1prVZ404yVo",
    )
    sheet_2_id = get_system_policy(
        "schedule_sheet_2_id",
        "1nFwb28bXa02Vcnnv9ORoDLkbAHFAYP3aDG9hsXiPTLM",
    )

    logger.info(
        "[sheets_sync] Синхронизация расписаний %d/%02d...", year, month,
    )

    try:
        client = _get_gspread_client(use_oauth2=False)
        sheet1 = client.open_by_key(sheet_1_id).worksheet("EXPORT")
        sheet2 = client.open_by_key(sheet_2_id).worksheet("EXPORT")
        data1 = sheet1.get_all_values()
        data2 = sheet2.get_all_values()
    except Exception as exc:
        logger.exception("[sheets_sync] Ошибка чтения Sheets: %s", exc)
        return {"error": str(exc)}

    dict1 = _build_full_month_dict(data1, last_day)
    dict2 = _build_full_month_dict(data2, last_day)

    operators = Operator.objects.select_related("group").all()
    stats = {"updated_days": 0, "skipped_operators": 0, "errors": 0}

    with transaction.atomic():
        for operator in operators:
            try:
                norm_fio = normalize_fio_for_match(
                    f"{operator.surname} {operator.name} {operator.middle_name or ''}",
                )

                # Grafik IKKALA sheetda ham tekshiriladi (group->sheet routing yo'q).
                # Operator qaysi sheetda bo'lsa ham topiladi — grafikda mavjud
                # bo'lgan operator skip bo'lib ketmaydi.
                days_data = dict1.get(norm_fio) or dict2.get(norm_fio)
                if not days_data:
                    stats["skipped_operators"] += 1
                    continue

                # Сохраняем по каждому дню
                for day_num_str, raw_value in days_data.items():
                    day = date(year, month, int(day_num_str))

                    if raw_value:
                        shift_obj = shift_service.find_shift_by_raw_value(raw_value)
                        is_day_off = shift_obj is None
                    else:
                        shift_obj = None
                        is_day_off = True

                    OperatorScheduleDay.objects.update_or_create(
                        operator=operator,
                        day=day,
                        defaults={
                            "shift": shift_obj,
                            "is_day_off": is_day_off,
                            "source": "sheets",
                            "raw_value": raw_value,
                        },
                    )
                    stats["updated_days"] += 1

            except Exception as exc:
                logger.exception(
                    "[sheets_sync] Ошибка для оператора %s: %s", operator, exc,
                )
                stats["errors"] += 1

    log_event(
        event_type="sheets_synced",
        level="info",
        message=f"Расписание синхронизировано за {year}/{month:02d}",
        payload=stats,
    )

    logger.info("[sheets_sync] Расписание синхронизировано: %s", stats)
    return stats


def _build_full_month_dict(sheet_data: list, last_day: int) -> dict:
    """
    Строит {normalized_fio: {day_num_str: raw_value}}.
    FIO во 3-й колонке (index=3), расписание начинается с 4-й (index=4).
    """
    result = {}
    FIO_COL = 3
    DAYS_START = 4

    for row in sheet_data:
        if len(row) <= FIO_COL:
            continue
        fio = normalize_fio_for_match(row[FIO_COL])
        if not fio:
            continue

        days_map = {}
        for day_num in range(1, last_day + 1):
            col_index = DAYS_START + (day_num - 1)
            if len(row) > col_index:
                value = row[col_index].strip()
                days_map[str(day_num)] = value if value else None
            else:
                days_map[str(day_num)] = None

        result[fio] = days_map

    return result