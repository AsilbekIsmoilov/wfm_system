from django.db import migrations


# Каталог подтипов: type_rule.code -> [ {code, display_name, full_day,
# requires_hour_range, sort_order}, ... ]. Идемпотентно (update_or_create).
SUBTYPES = {
    "lgota_prochie": [
        {"code": "otgul", "display_name": "Отгул",
         "full_day": True, "requires_hour_range": False, "sort_order": 10},
        {"code": "korotkiy_den", "display_name": "Короткий день",
         "full_day": False, "requires_hour_range": True, "sort_order": 20},
    ],
}


def seed_and_migrate(apps, schema_editor):
    RequestTypeRule = apps.get_model("hourly_locks", "RequestTypeRule")
    RequestSubtypeRule = apps.get_model("hourly_locks", "RequestSubtypeRule")
    Transfer = apps.get_model("hourly_locks", "Transfer")

    # 1. Засеять подтипы под соответствующие типы (transfer-категория).
    for type_code, subs in SUBTYPES.items():
        rule = RequestTypeRule.objects.filter(
            category="transfer", code=type_code).first()
        if not rule:
            continue
        for s in subs:
            RequestSubtypeRule.objects.update_or_create(
                type_rule=rule, code=s["code"],
                defaults={
                    "display_name": s["display_name"],
                    "full_day": s["full_day"],
                    "requires_hour_range": s["requires_hour_range"],
                    "sort_order": s["sort_order"],
                    "is_active": True,
                },
            )

    # 2. Перенести существующие строковые subtype в FK subtype_rule (по паре
    #    type_rule + code заявки).
    for tr in Transfer.objects.exclude(subtype__isnull=True).exclude(subtype="").iterator():
        sub = RequestSubtypeRule.objects.filter(
            type_rule_id=tr.type_rule_id, code=tr.subtype).first()
        if sub:
            tr.subtype_rule_id = sub.id
            tr.save(update_fields=["subtype_rule"])


def reverse(apps, schema_editor):
    # Вернуть строковый subtype из FK (на случай отката).
    Transfer = apps.get_model("hourly_locks", "Transfer")
    for tr in Transfer.objects.exclude(subtype_rule__isnull=True).iterator():
        tr.subtype = tr.subtype_rule.code
        tr.save(update_fields=["subtype"])


class Migration(migrations.Migration):

    dependencies = [
        ("hourly_locks", "0059_alter_transfer_subtype_requestsubtyperule_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_and_migrate, reverse),
    ]
