from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hourly_locks', '0055_alter_compensation_correlation_id_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='RequestActionLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('wfm_id', models.CharField(help_text='Mongo _id лога в WFM — ключ идемпотентности при push.', max_length=64, unique=True, verbose_name='WFM лог _id')),
                ('user_id', models.CharField(blank=True, default='', max_length=64, verbose_name='WFM user _id')),
                ('user_name', models.CharField(blank=True, default='', max_length=255, verbose_name='Пользователь')),
                ('method', models.CharField(choices=[('POST', 'POST'), ('PATCH', 'PATCH'), ('GET', 'GET'), ('DELETE', 'DELETE')], max_length=10, verbose_name='Метод')),
                ('entity_type', models.CharField(choices=[('compensation', 'compensation'), ('transfer', 'transfer')], max_length=20, verbose_name='Тип заявки')),
                ('entity_external_id', models.BigIntegerField(blank=True, help_text='external_id заявки; null для GET-списка.', null=True, verbose_name='ID заявки')),
                ('endpoint', models.CharField(max_length=500, verbose_name='Endpoint')),
                ('payload', models.JSONField(blank=True, default=dict, verbose_name='Данные')),
                ('status_code', models.PositiveSmallIntegerField(blank=True, null=True, verbose_name='HTTP статус')),
                ('success', models.BooleanField(default=True, verbose_name='Успех')),
                ('created_at', models.DateTimeField(verbose_name='Дата/время')),
            ],
            options={
                'verbose_name': 'Лог действия над заявкой',
                'verbose_name_plural': 'Логи действий над заявками',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='requestactionlog',
            index=models.Index(fields=['user_id'], name='hl_ral_user_idx'),
        ),
        migrations.AddIndex(
            model_name='requestactionlog',
            index=models.Index(fields=['entity_type', 'entity_external_id'], name='hl_ral_entity_idx'),
        ),
        migrations.AddIndex(
            model_name='requestactionlog',
            index=models.Index(fields=['created_at'], name='hl_ral_created_idx'),
        ),
        migrations.AddIndex(
            model_name='requestactionlog',
            index=models.Index(fields=['method'], name='hl_ral_method_idx'),
        ),
    ]
