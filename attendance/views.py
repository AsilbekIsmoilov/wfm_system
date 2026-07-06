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



from django.shortcuts import render
from django.views import View


class AttendanceTestView(View):

    def get(self, request):
        return render(request, "attendance_test.html")


def board(request):
    """Jonli davomat board'i (WebSocket ws/attendance/ orqali yangilanadi)."""
    return render(request, "attendance/board.html")