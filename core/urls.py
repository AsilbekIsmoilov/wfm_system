from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from hourly_locks.sync.apply_view import (
    ApplyRequestView,        # WFM -> Python apply
    EditCompensationView,    # WFM -> Python PATCH compensation (edit)
    EditTransferView,        # WFM -> Python PATCH transfer (edit)
)
from hourly_locks.sync.sync_ingest import UpsertView, DeleteView  # WFM -> Python CRUD


urlpatterns = [
    path('admin/', admin.site.urls),
    path('api-auth/', include('rest_framework.urls')),
    path('api/sync/apply-request', ApplyRequestView.as_view()),
    path('api/sync/edit-compensation', EditCompensationView.as_view()),
    path('api/sync/edit-transfer', EditTransferView.as_view()),
    path('api/sync/upsert/<entity>', UpsertView.as_view()),
    path('api/sync/delete/<entity>', DeleteView.as_view()),
    path('', include('hourly_locks.urls')),
    path('api/attendance/v1/', include('attendance.urls')),

]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)