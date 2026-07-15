"""
Сигналы Django для инвалидации кэша и синхронизации связанных моделей.

Бизнес-логика В СИГНАЛЫ НЕ ВЫНОСИТСЯ — она в сервисах.
Здесь только:
  - инвалидация кэша
  - синхронизация User.role ↔ Operator (через свойство)
  - инвалидация SystemPolicy кэша
"""

import logging

from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import (
    Compensation,
    SystemPolicy,
    Transfer,
    WorkDebt,
    WorkDebtDetail,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Инвалидация кэша списков долгов
# =============================================================================

@receiver([post_save, post_delete], sender=WorkDebt)
@receiver([post_save, post_delete], sender=WorkDebtDetail)
@receiver([post_save, post_delete], sender=Compensation)
@receiver([post_save, post_delete], sender=Transfer)
def clear_debt_related_cache(sender, **kwargs):
    """
    При изменении WorkDebt, WorkDebtDetail, Compensation или Transfer
    сбрасываем кэшированные списки долгов.
    """
    try:
        patterns = [
            "debts:list:*",
            "debts:list_light:*",
            "operator_history:*",
        ]

        deleted = 0
        for pattern in patterns:
            if hasattr(cache, "delete_pattern"):
                cache.delete_pattern(pattern)
            else:
                # Fallback: ограниченный перебор
                for user_id in range(1, 1000):
                    key = pattern.replace("*", f"u{user_id}")
                    cache.delete(key)
                    deleted += 1

        logger.debug(
            "[CACHE] Сброшен кэш долгов (sender=%s)",
            sender.__name__,
        )

    except Exception as exc:
        logger.error("[CACHE] Ошибка сброса кэша: %s", exc)


# =============================================================================
# Инвалидация кэша системных политик
# =============================================================================

@receiver([post_save, post_delete], sender=SystemPolicy)
def clear_system_policy_cache(sender, instance, **kwargs):
    """Сбрасываем кэш конкретной политики при её изменении."""
    cache.delete(f"system_policy:{instance.key}")
    logger.debug("[CACHE] Сброшен кэш SystemPolicy '%s'", instance.key)


# =============================================================================
# WorkDebt konsistentligi: WDD o'chirilganda WorkDebt'ni qayta hisoblash
# =============================================================================

# Ommaviy o'chirishда (arxivlash) har WDD uchun recompute qilmaslik uchun toggle.
_skip_wdd_delete_recompute = False


def skip_wdd_delete_recompute(value: bool):
    """Arxivlash/ommaviy tozalash paytida WDD-delete signal-recompute'ni o'chirish."""
    global _skip_wdd_delete_recompute
    _skip_wdd_delete_recompute = value


@receiver(post_delete, sender=WorkDebtDetail)
def recompute_workdebt_on_wdd_delete(sender, instance, **kwargs):
    """
    WDD o'chirilganda operatorning WorkDebt'ini qayta hisoblaydi.
    Kafolat: WDD yo'q bo'lsa (yoki kamaysa) WorkDebt.current_debt = 0 (yoki mos qiymat).
    Aks holda WDD o'chib, WorkDebt eski qiymatda qolib ketardi (stale).
    Ommaviy tozalash (arxivlash) paytida skip_wdd_delete_recompute(True) bilan o'chiriladi.
    """
    if _skip_wdd_delete_recompute:
        return
    op_id = getattr(instance, "operator_id", None)
    cyc_id = getattr(instance, "cycle_id", None)
    if not op_id or not cyc_id:
        return
    try:
        from .models import Cycle
        from .services.work_debt import recompute_for_operators
        cycle = Cycle.objects.filter(id=cyc_id).first()
        if cycle:
            recompute_for_operators([op_id], cycle)
    except Exception as exc:
        logger.exception("[wdd_delete_recompute] Xato (op=%s): %s", op_id, exc)


# =============================================================================
# Auto-approve Compensation pri sozdanii (universal — admin/API/ORM)
# =============================================================================

@receiver(post_save, sender=Compensation)
def auto_process_compensation_on_create(sender, instance, created, **kwargs):
    """
    Yangi Compensation yaratilganda avtomatik ishlov:
      1. auto_approve_on_create=True → darhol tasdiqlaydi
      2. retroactive_check strategy → auto_check ishga tushiradi
         (status pending'da qoladi, lekin auto_check_result to'ldiriladi)

    Bu signal admin paneldan, API dan, ORM yoki management command dan
    yaratishni ham qamrab oladi.
    """
    if not created:
        return
    if instance.status != "pending":
        return

    try:
        rule = instance.type_rule
        if not rule:
            return

        from .services.compensation_verifier import verify_single_compensation
        from .services.cycle import get_or_create_active_cycle

        # Auto-approve yoki retroactive_check — verify_single chaqiriladi
        # (verify_single_compensation ichida strategy'ga qarab dispetcherlik)
        is_auto = rule.auto_approve_on_create or rule.verification_strategy in (
            "auto_approve",
            "retroactive_check",
        )
        if not is_auto:
            return

        cycle = instance.cycle or get_or_create_active_cycle()
        result = verify_single_compensation(instance, instance.planned_date, cycle)
        logger.info(
            "[auto_process_signal] Compensation #%d (%s, %s) -> %s",
            instance.id, rule.code, rule.verification_strategy, result,
        )
    except Exception as exc:
        logger.exception("[auto_process_signal] Xato: %s", exc)