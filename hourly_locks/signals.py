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
from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver

from .models import (
    Compensation,
    Group,
    SystemPolicy,
    Transfer,
    WorkDebt,
    WorkDebtDetail,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Инвалидация кэша исключённых групп (group_scope)
# =============================================================================

@receiver([post_save, post_delete], sender=Group)
def invalidate_excluded_groups_cache(sender, instance, **kwargs):
    """Сброс кэша deny-list при любом изменении группы (напр. переключение
    is_excluded в админке), чтобы шлюз сразу увидел новое состояние."""
    from .services.group_scope import invalidate_cache
    invalidate_cache()


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
# Удаление заявки-компенсации: возврат (разблокировка) её WDD + пересчёт долга
# =============================================================================

@receiver(pre_delete, sender=Compensation)
def revert_compensation_on_delete(sender, instance, **kwargs):
    """ДО удаления заявки: разблокировать ВСЕ её WDD и удалить запись-остаток
    (пока связки ещё существуют). Пересчёт долга — в post_delete (после каскада).
    Покрывает любой путь удаления: WFM-sync, админка, ORM.
    При архивации связки уже удалены раньше → фактически no-op."""
    if _skip_wdd_delete_recompute:
        return
    try:
        from .services.compensation_edit import revert_compensation_for_delete
        revert_compensation_for_delete(instance)
    except Exception as exc:
        logger.exception("[compensation_delete_revert] Xato (comp=%s): %s",
                         getattr(instance, "id", None), exc)


@receiver(post_delete, sender=Compensation)
def recompute_on_compensation_delete(sender, instance, **kwargs):
    """ПОСЛЕ удаления заявки (applied-связки удалены каскадом) — пересчитать долг
    оператора: снятый заявкой зачёт возвращается в current_debt."""
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
        logger.exception("[compensation_delete_recompute] Xato (op=%s): %s", op_id, exc)


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