import logging
from datetime import date, datetime, timedelta

from celery import shared_task
from django.utils.timezone import localdate

from .services import (
    cycle as cycle_service,
    debt_calculator,
    compensation_verifier,
    log_loader,
    operator_sync,
    sheets_sync,
    transfer_verifier,
)
from .services.event_log import log_event
from .services.external_api import check_api_integrity
from .sync.runners_sync import push_cycle_to_wfm  # WFM push (Oqim C)

logger = logging.getLogger(__name__)



@shared_task(name="hourly_locks.sync_groups")
def sync_groups_task():
    """Синхронизация групп из Google Sheets."""
    logger.info("[task] sync_groups запущена")
    result = sheets_sync.sync_groups_from_sheets()
    logger.info("[task] sync_groups завершена: %s", result)
    return result


@shared_task(name="hourly_locks.sync_schedules")
def sync_schedules_task(year: int = None, month: int = None):
    """Синхронизация расписания за месяц."""
    logger.info("[task] sync_schedules запущена")
    target_month = date(year, month, 1) if year and month else None
    result = sheets_sync.sync_schedules_from_sheets(target_month=target_month)
    logger.info("[task] sync_schedules завершена: %s", result)
    return result


# =============================================================================
# Ежедневный конвейер
# =============================================================================

@shared_task(name="hourly_locks.daily_pipeline")
def daily_pipeline_task(target_date_iso: str = None, skip_sheets: bool = False, full: bool = False):
    """ЕДИНЫЙ ежедневный конвейер.

    Делегирует в daily_runner.run() — ЕДИНЫЙ источник истины (тот же код, что и
    при ручном запуске `python daily_runner.py`). Никаких расхождений.
    Ленивый импорт: daily_runner делает django.setup() на уровне модуля, поэтому
    импортируем внутри задачи (django уже инициализирован в воркере).
    """
    if target_date_iso:
        target_date = date.fromisoformat(target_date_iso)
    else:
        target_date = localdate() - timedelta(days=1)

    logger.info("[daily_pipeline] Старт за %s", target_date)
    from daily_runner import run as run_daily
    result = run_daily(target_date, skip_sheets=skip_sheets, full=full)
    logger.info("[daily_pipeline] Завершено: %s", result)
    return result


# =============================================================================
# Авто-проверка заявок отработки
# =============================================================================

@shared_task(name="hourly_locks.verify_otrabotka")
def verify_otrabotka_task(target_date_iso: str = None):
    """Проверить заявки отработки, у которых все дни прошли."""
    from .services.otrabotka_verify import verify_due_otrabotka

    target_date = date.fromisoformat(target_date_iso) if target_date_iso else None
    result = verify_due_otrabotka(target_date)
    logger.info("[verify_otrabotka_task] Результат: %s", result)
    return result


# =============================================================================
# Авто-закрытие цикла
# =============================================================================

@shared_task(name="hourly_locks.auto_close_cycle")
def auto_close_cycle_task():

    logger.info("[auto_close_cycle_task] Старт")
    result = cycle_service.ensure_active_cycle()


    try:
        push_cycle_to_wfm()
    except Exception as exc:
        logger.exception("[auto_close_cycle_task] WFM push xatosi: %s", exc)

    logger.info("[auto_close_cycle_task] Результат: %s", result)
    return result