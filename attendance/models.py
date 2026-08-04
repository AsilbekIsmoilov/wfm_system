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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("operator", "date")
        indexes = [
            models.Index(fields=["date"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.operator} | {self.date} | {self.status}"