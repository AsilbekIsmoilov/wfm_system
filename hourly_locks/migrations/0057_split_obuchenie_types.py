# Data migration: retire "Обучение" (obuchenie) and replace it with two types
# that share the SAME benefit logic — "Обучение на службу" and "Обучение по ошибкам".
# Old obuchenie is kept but deactivated (is_active=False) so existing requests stay
# valid; the new types are created active in BOTH categories (compensation + transfer).
from django.db import migrations


# (code [english], display_name [russian], sort_order)
NEW_TYPES = [
    ("training_on_duty", "Обучение на службу", 71),
    ("training_on_errors", "Обучение по ошибкам", 72),
]
CATEGORIES = ["compensation", "transfer"]

BENEFIT_FLAGS = dict(
    creates_debt_if_unmet=False,
    exempts_from_daily_debt=True,
    auto_approve_on_create=True,
    allows_past_date=True,
    verification_strategy="auto_approve",
)


def forward(apps, schema_editor):
    RequestTypeRule = apps.get_model("hourly_locks", "RequestTypeRule")
    # Retire the old "Обучение" (kept for history; hidden from new requests).
    RequestTypeRule.objects.filter(code="obuchenie").update(is_active=False)
    # Add the two replacement types (same benefit logic) in both categories.
    for category in CATEGORIES:
        for code, display_name, sort_order in NEW_TYPES:
            RequestTypeRule.objects.update_or_create(
                category=category,
                code=code,
                defaults={
                    "display_name": display_name,
                    "sort_order": sort_order,
                    "is_active": True,
                    **BENEFIT_FLAGS,
                },
            )


def backward(apps, schema_editor):
    RequestTypeRule = apps.get_model("hourly_locks", "RequestTypeRule")
    RequestTypeRule.objects.filter(code__in=[c for c, _, _ in NEW_TYPES]).delete()
    RequestTypeRule.objects.filter(code="obuchenie").update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ("hourly_locks", "0056_requestactionlog"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
