from django.conf import settings
from rest_framework.permissions import BasePermission


class HasServiceToken(BasePermission):
    """DRF permission: allow only requests carrying the correct service token."""
    message = "Invalid or missing sync service token"

    def has_permission(self, request, view):
        token = request.headers.get("x-sync-token")
        expected = getattr(settings, "SYNC_SERVICE_TOKEN", None)
        return bool(expected) and token == expected


class HasServiceTokenOrQuery(BasePermission):
    """Как HasServiceToken, но токен можно передать и через query (?token=…) —
    для удобного просмотра логов прямо в браузере."""
    message = "Invalid or missing sync service token"

    def has_permission(self, request, view):
        token = (
            request.headers.get("x-sync-token")
            or request.query_params.get("token")
        )
        expected = getattr(settings, "SYNC_SERVICE_TOKEN", None)
        return bool(expected) and token == expected
