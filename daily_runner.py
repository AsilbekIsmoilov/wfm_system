"""
daily_runner.py — ЕДИНЫЙ ежедневный конвейер (один прогон в день, 09:00).

Окна загрузки логов теперь 24-часовые (отработка/учёба/ночь 20-08 уже учтены
расширенными окнами внутри log_loader/verify), поэтому один прогон в день
покрывает ВСЁ без конфликтов — отдельный ночной конвейер больше не нужен.

ЕДИНЫЙ ИСТОЧНИК ИСТИНЫ: и Celery-beat (tasks.daily_pipeline_task), и ручной
запуск (`python daily_runner.py`, `manage.py run_daily_pipeline`) вызывают
ОДНУ функцию run() — расхождений быть не может.

Порядок:
  1. График (Google Sheets: группы + расписание)
  2. Проверка готовности API
  3. Авто-создание операторов из API
  4. Обновление статусов Transfer (pending → in_progress → completed)
  5. Загрузка логов (24ч-окна)
  6. Расчёт долгов (рабочее время)
  6.5 Отпрашивание: недоработку дней отгула → долг time_off + привязка к плану
  7. Проверка компенсаций
  7.5 Проверка заявок отработки (otrabotka)
  7.6 Учёба в рабочее время (ucheba) — норма по расширенному окну
  8. Проверка переносов (transfers)
  9. Отгулы (расширенное окно)
  10. Финальный пересчёт долгов (safety net)
  11. Push в WFM (Oqim C, инкрементально)

Использование:
  python daily_runner.py                 # за вчера
  python daily_runner.py 2026-06-29      # за конкретную дату
  python daily_runner.py --skip-sheets   # без синхронизации Sheets
"""
import logging
import os
import sys
from datetime import date, datetime, timedelta

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()

from django.utils.timezone import localdate  # noqa: E402

from hourly_locks.services import (  # noqa: E402
    compensation_verifier,
    debt_calculator,
    log_loader,
    operator_sync,
    sheets_sync,
    transfer_verifier,
)
from hourly_locks.services.event_log import log_event  # noqa: E402
from hourly_locks.services.external_api import check_api_integrity  # noqa: E402
from hourly_locks.sync.runners_sync import push_cycle_to_wfm  # noqa: E402  # WFM push (Oqim C)


logger = logging.getLogger("daily_runner")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def section(title: str):
    logger.info("=" * 60)
    logger.info(title)
    logger.info("=" * 60)


def _step(result: dict, key: str, title: str, fn):
    """Выполнить один шаг с логом и перехватом ошибки (батч не падает целиком)."""
    section(title)
    try:
        result[key] = fn()
        logger.info("%s: %s", key, result[key])
    except Exception as exc:
        logger.exception("%s xatosi: %s", key, exc)
        result[key] = {"error": str(exc)}
    return result[key]


# Окно синхронизации графика: только со 2-го по 22-е число месяца включительно.
# Вне окна (23-е … 1-е след. месяца) график НЕ обновляется — работает по
# последнему обновлению 22-го числа (как в старой системе).
GRAFIK_SYNC_START_DAY = 2
GRAFIK_SYNC_END_DAY = 22


def _in_grafik_window(day: date) -> bool:
    return GRAFIK_SYNC_START_DAY <= day.day <= GRAFIK_SYNC_END_DAY


def run(target_date: date, skip_sheets: bool = False) -> dict:
    """ЕДИНЫЙ ежедневный прогон за target_date. Вызывается и beat'ом, и вручную."""
    result = {"target_date": str(target_date)}
    section(f"DAILY RUNNER za {target_date}")

    # 1. График (Google Sheets: группы + расписание) — ТОЛЬКО в окне 2–22 числа.
    #    Вне окна график заморожен (последнее обновление 22-го). Окно проверяем
    #    по РЕАЛЬНОМУ сегодня (localdate), а не по target_date.
    run_day = localdate()
    if skip_sheets:
        logger.info("Sheets sync o'tkazib yuborildi (--skip-sheets)")
        result["grafik_sync"] = "skipped_flag"
    elif not _in_grafik_window(run_day):
        logger.info(
            "Grafik sync o'tkazib yuborildi: bugun %s (%d-son) — %d–%d oynasidan "
            "tashqarida; grafik %d-sanadagi oxirgi holatda qotgan.",
            run_day, run_day.day,
            GRAFIK_SYNC_START_DAY, GRAFIK_SYNC_END_DAY, GRAFIK_SYNC_END_DAY,
        )
        result["grafik_sync"] = "skipped_out_of_window"
    else:
        _step(result, "sync_groups", "1. Grafik: guruhlar (Sheets)",
              sheets_sync.sync_groups_from_sheets)
        _step(result, "sync_schedules", "1. Grafik: jadval (Sheets)",
              sheets_sync.sync_schedules_from_sheets)

    # 2. Проверка готовности API — если не готов, конвейер останавливается
    section("2. API integrity tekshirish")
    integrity = check_api_integrity(target_date)
    result["api_integrity"] = integrity
    if not integrity["ok"]:
        logger.error(
            "API tayyor emas (%d/24 soat ishlamayapti). Pipeline to'xtatildi.",
            integrity["error_count"],
        )
        log_event(
            event_type="api_error",
            level="error",
            message=f"API nedostupen dlya {target_date}: {integrity['errors']}",
            payload=integrity,
        )
        result["status"] = "stopped_api_error"
        return result
    logger.info("API OK: %d/24 soat", integrity["ok_count"])

    # 3. Авто-создание операторов из API
    _step(result, "operator_sync", "3. Operatorlarni avto-yaratish",
          lambda: operator_sync.sync_operators_from_api(target_date))

    # 4. Обновление статусов Transfer
    _step(result, "transfer_statuses", "4. Transfer statuslarini yangilash",
          lambda: transfer_verifier.update_transfer_statuses(today=localdate()))

    # 5. Загрузка логов (24ч-окна)
    _step(result, "logs", f"5. Loglarni yuklash za {target_date}",
          lambda: log_loader.load_logs_for_day(target_date))

    # 6. Расчёт долгов (рабочее время)
    _step(result, "debts", f"6. Qarzlarni hisoblash za {target_date}",
          lambda: debt_calculator.calculate_work_debts(target_date))

    # 6.5 Отпрашивание: недоработку дней отгула → долг time_off + привязка к плану
    #     (ПОСЛЕ debt_calculator).
    def _otpr():
        from hourly_locks.services.otprashivanie import process_otprashivanie_debts
        return process_otprashivanie_debts(target_date)
    _step(result, "otprashivanie", f"6.5 Otprashivanie долги za {target_date}", _otpr)

    # 7. Проверка компенсаций
    _step(result, "compensations", f"7. Compensations za {target_date}",
          lambda: compensation_verifier.verify_compensations(target_date))

    # 7.5 Проверка заявок отработки (otrabotka) — дни которых прошли
    def _otrabotka():
        from hourly_locks.services.otrabotka_verify import verify_due_otrabotka
        return verify_due_otrabotka(target_date)
    _step(result, "otrabotka", f"7.5 Otrabotka tekshiruvi za {target_date}", _otrabotka)

    # 7.6 Учёба в рабочее время — дневная норма по расширенному окну
    def _ucheba():
        from hourly_locks.services.ucheba import verify_due_ucheba
        return verify_due_ucheba(target_date)
    _step(result, "ucheba", f"7.6 Ucheba tekshiruvi za {target_date}", _ucheba)

    # 8. Проверка переносов
    _step(result, "transfers", f"8. Transfers za {target_date}",
          lambda: transfer_verifier.verify_transfers_for_date(target_date))

    # 9. Отгулы (расширенное окно)
    _step(result, "time_offs", f"9. Time-off za {target_date}",
          lambda: transfer_verifier.apply_time_off_extended_window(target_date))

    # 10. Финальный пересчёт долгов (safety net)
    def _recompute():
        from hourly_locks.services.work_debt import recompute_for_date
        return recompute_for_date(target_date)
    _step(result, "work_debt_recompute", f"10. Yakuniy recompute za {target_date}", _recompute)

    result["status"] = "completed"

    # Yakuniy event
    log_event(
        event_type="log_loaded",
        level="info",
        message=f"Daily runner zavershen za {target_date}",
        payload={k: v for k, v in result.items() if k not in ("api_integrity",)},
    )

    # 11. WFM ga natijani yuborish (Oqim C, инкрементально) — ПЕРЕД ролловером,
    #     чтобы данные последнего дня цикла ушли в WFM до архивации.
    _step(result, "wfm_push", "11. WFM push (Oqim C)", push_cycle_to_wfm)

    # 12. Ролловер цикла: закрыть истёкший (end_date < сегодня) и открыть новый
    #     активный. Только для ШТАТНОГО прогона (target = вчера) — для ручного
    #     backfill произвольной даты цикл НЕ трогаем. Выполняется ПОСЛЕ обработки
    #     последнего дня цикла → на 20-е число старый цикл (end=19) закрывается.
    if target_date == localdate() - timedelta(days=1):
        def _cycle():
            from hourly_locks.services.cycle import ensure_active_cycle
            return ensure_active_cycle(localdate())
        _step(result, "cycle_rollover", "12. Tsikl (yopish/ochish)", _cycle)

    section(f"GOTOVO za {target_date}")
    return result


def parse_args() -> dict:
    args = {
        "target_date": localdate() - timedelta(days=1),
        "skip_sheets": False,
    }
    for arg in sys.argv[1:]:
        if arg == "--skip-sheets":
            args["skip_sheets"] = True
        elif arg in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        else:
            try:
                args["target_date"] = datetime.strptime(arg, "%Y-%m-%d").date()
            except ValueError:
                logger.error("Noma'lum argument: %s", arg)
                print(__doc__)
                sys.exit(1)
    return args


if __name__ == "__main__":
    args = parse_args()
    result = run(args["target_date"], skip_sheets=args["skip_sheets"])
    logger.info("Final result: %s", result)
