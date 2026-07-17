from django.db import migrations


class Migration(migrations.Migration):
    """Слияние двух ветвей 0057 (entity_wfm_id + split_obuchenie_types)."""

    dependencies = [
        ('hourly_locks', '0057_rename_hl_ral_user_idx_hourly_lock_user_id_14dd71_idx_and_more'),
        ('hourly_locks', '0057_split_obuchenie_types'),
    ]

    operations = []
