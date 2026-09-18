from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("attendance", "0003_attendancelog_comment_attendancelog_updated_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="attendancelog",
            name="comment_created_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Комментарий создан"),
        ),
        migrations.AddField(
            model_name="attendancelog",
            name="comment_created_by",
            field=models.CharField(
                blank=True, default="", max_length=255,
                verbose_name="Комментарий создал"),
        ),
        migrations.AddField(
            model_name="attendancelog",
            name="comment_updated_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Комментарий изменён"),
        ),
        migrations.AddField(
            model_name="attendancelog",
            name="comment_updated_by",
            field=models.CharField(
                blank=True, default="", max_length=255,
                verbose_name="Комментарий изменил"),
        ),
    ]
