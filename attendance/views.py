# from rest_framework.views import APIView
# from rest_framework.response import Response
# from .services.attendance import *
#
#
# class TodayAttendanceView(APIView):
#     def get(self, request):
#         schedule_filter = request.GET.get("schedule")
#         start_filter = request.GET.get("start")
#         status_filter = request.GET.get("status")
#
#         data = build_attendance()
#         filtered = []
#
#         schedules = None
#         if schedule_filter:
#             schedules = [s.strip() for s in schedule_filter.split(",") if s.strip()]
#
#         statuses = None
#         if status_filter:
#             statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
#
#         for item in data:
#             item = item.copy()
#
#             if schedules and item["schedule"] not in schedules:
#                 continue
#
#             if statuses:
#                 if item["status"] is None:
#                     continue
#
#                 if item["status"] not in statuses:
#                     continue
#
#             if start_filter:
#                 if not item["scheduled_start"].startswith(start_filter.zfill(2)):
#                     continue
#
#             item.pop("scheduled_start_dt", None)
#             filtered.append(item)
#
#         return Response(filtered)



from datetime import date as _date, datetime

from django.shortcuts import render
from django.utils import timezone
from django.views import View
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status as http_status

from hourly_locks.models import Operator, OperatorScheduleDay
from hourly_locks.sync.service_auth import HasServiceToken
from .models import AttendanceLog


class AttendanceTestView(View):

    def get(self, request):
        return render(request, "attendance_test.html")


def board(request):
    """Jonli davomat board'i (WebSocket ws/attendance/ orqali yangilanadi)."""
    return render(request, "attendance/board.html")


class AttendanceCommentView(APIView):
    """WFM -> Django: комментарий супервайзера к строке оператора за день
    (latecomers). Комментарий пишется во фронте, сохраняется в WFM и
    синхронизируется сюда.

    POST body: {"operator_id": <Django operator id>, "date": "YYYY-MM-DD",
                "comment": "..."}
    Строка AttendanceLog (operator, date) создаётся при отсутствии.
    """
    permission_classes = [HasServiceToken]

    def post(self, request):
        data = request.data or {}
        op_id = data.get("operator_id")
        login = data.get("login")
        day_str = data.get("date")
        comment = data.get("comment") or ""
        if (not op_id and not login) or not day_str:
            return Response(
                {"ok": False, "message": "operator_id yoki login, va date shart"},
                status=http_status.HTTP_400_BAD_REQUEST)
        try:
            day = _date.fromisoformat(str(day_str)[:10])
        except ValueError:
            return Response({"ok": False, "message": "date format: YYYY-MM-DD"},
                            status=http_status.HTTP_400_BAD_REQUEST)
        if op_id:
            operator = Operator.objects.filter(id=op_id).first()
        else:
            operator = Operator.objects.filter(login_id=str(login)).first()
        if not operator:
            return Response(
                {"ok": False, "message": f"operator topilmadi: {op_id or login}"},
                status=http_status.HTTP_404_NOT_FOUND)

        log = AttendanceLog.objects.filter(operator=operator, date=day).first()
        if log:
            log.comment = comment
            log.save(update_fields=["comment", "updated_at"])
        else:
            # Строки ещё нет (не логинился/не отмечен) — создаём, чтобы сохранить
            # комментарий. scheduled_start берём из графика (или начало дня).
            sd = (OperatorScheduleDay.objects
                  .filter(operator=operator, day=day)
                  .select_related("shift").first())
            if sd and sd.shift and sd.shift.start_time:
                sched = timezone.make_aware(
                    datetime.combine(day, sd.shift.start_time))
            else:
                sched = timezone.make_aware(
                    datetime.combine(day, datetime.min.time()))
            log = AttendanceLog.objects.create(
                operator=operator, date=day, scheduled_start=sched,
                status="absent", comment=comment,
            )
        return Response({"ok": True, "id": log.id, "comment": log.comment})