"""
HTTP client to the WFM platform (NestJS).
 - pull_changes(): Oqim B — fetch WFM-side changes (called at runner start).
 - push_work_debts() / push_work_debt_details() / push_cycles() / push_request_results():
   Oqim C — push runner results back to WFM (called at runner end).

Note: WFM wraps responses in a unified envelope, so GET responses are unwrapped below.
"""
import requests
from django.conf import settings


def _headers():
    return {
        "x-sync-token": settings.SYNC_SERVICE_TOKEN,
        "content-type": "application/json",
    }


def _base():
    return settings.WFM_BASE_URL.rstrip("/")


# ---------------------------------------------------------------------------
# Oqim B — pull changes from WFM
# ---------------------------------------------------------------------------
def pull_changes(since_iso, entities="operators,compensations,transfers", limit=500):
    """
    Returns a dict like: { "operators": [...], "compensations": [...], "transfers": [...] }
    """
    resp = requests.get(
        f"{_base()}/sync/changes",
        params={"since": since_iso, "limit": limit, "entities": entities},
        headers=_headers(),
        timeout=settings.WFM_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    # Unwrap WFM's response envelope defensively:
    inner = payload.get("data", payload) if isinstance(payload, dict) else payload
    changes = inner.get("data", inner) if isinstance(inner, dict) else inner
    return changes or {}


# ---------------------------------------------------------------------------
# Oqim C — push runner results to WFM
# ---------------------------------------------------------------------------
def _post(path, items):
    resp = requests.post(
        f"{_base()}/{path}",
        json=items,
        headers=_headers(),
        timeout=settings.WFM_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def push_work_debts(items):
    """items: list of dicts with operator_id, cycle_id, current_debt, total_accumulated, external_id?"""
    return _post("sync/work-debts", items)


def push_work_debt_details(items):
    """items: list of dicts (each must include external_id = Django PK)."""
    return _post("sync/work-debt-details", items)


def push_cycles(items):
    return _post("sync/cycles", items)


def push(entity, items):
    """Generic bulk upsert for ANY entity (POST /sync/ingest/<entity>)."""
    return _post(f"sync/ingest/{entity}", items)


def reconcile(entity, body):
    """
    Delete-sync/prune: WFM'da berilgan scope ichida `external_ids` ro'yxatida
    BO'LMAGAN yozuvlarni o'chiradi (POST /sync/reconcile/<entity>).
    body: {"external_ids": [...], "cycle_id": <id>} yoki {"compensation_ids": [...]}.
    """
    return _post(f"sync/reconcile/{entity}", body)


def push_request_results(items):
    """
    items: list of dicts like
      { "category": "compensation"|"transfer", "external_id": <pk>,
        "status": "...", "verified_duration": ..., "remaining_debt": ... }
    """
    return _post("sync/requests/result", items)
