from django.db import models


class AttendanceLog(models.Model):
    STATUS_CHOICES = [
        ("on_time", "On Time"),
        ("late", "Late"),
        ("absent", "Absent"),
        ("early_login", "Early Login"),
    ]

    operator = models.ForeignKey(
        "hourly_locks.Operator",
        on_delete=models.CASCADE,
        related_name="attendance_logs"
    )
    date = models.DateField(db_index=True)
    scheduled_start = models.DateTimeField()
    login_time = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=11, choices=STATUS_CHOICES)
    delay_minutes = models.IntegerField(default=0)
    # Комментарий супервайзера к строке оператора (на этот день). Пишется во
    # фронте (WFM), сохраняется в WFM и синхронизируется сюда. Дату не передаём —
    # это всегда текущий день; аудит (кто/когда создал и изменил) проставляется
    # автоматически.
    comment = models.TextField(verbose_name="Комментарий", blank=True, default="")
    comment_created_at = models.DateTimeField(
        verbose_name="Комментарий создан", null=True, blank=True)
    comment_created_by = models.CharField(
        verbose_name="Комментарий создал", max_length=255, blank=True, default="")
    comment_updated_at = models.DateTimeField(
        verbose_name="Комментарий изменён", null=True, blank=True)
    comment_updated_by = models.CharField(
        verbose_name="Комментарий изменил", max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("operator", "date")
        indexes = [
            models.Index(fields=["date"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.operator} | {self.date} | {self.status}"