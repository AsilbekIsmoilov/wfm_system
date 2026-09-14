from django.urls import path
from .views import *

urlpatterns = [
    # path("today/", TodayAttendanceView.as_view()),
    path("attendance-test/", AttendanceTestView.as_view()),
    path("board/", board, name="attendance_board"),
    # WFM -> Django: комментарий к строке оператора за день (latecomers)
    path("comment/", AttendanceCommentView.as_view(), name="attendance_comment"),
]