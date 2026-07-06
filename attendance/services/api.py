"""
Jonli operator snapshotini WFM'dan olish (WFM /operators/live).

Auth: JWT — WFM'ga login (login/parol) qilib accessToken olamiz, tokenni
keshda saqlaymiz; token eskirsa (401) bir marta qayta login qilamiz.

Javob shakli attendance kutgani bilan bir xil: {"data": {"agents": [...]}}.
"""
import requests
from django.conf import settings

# Token keshi (jarayon ichida). Har so'rovda login qilmaslik uchun.
_token_cache = {"token": None}


def _base():
    return settings.WFM_BASE_URL.rstrip("/")


def _login() -> str:
    """WFM'ga login qilib accessToken qaytaradi."""
    resp = requests.post(
        f"{_base()}/auth/login",
        json={"login": settings.WFM_LOGIN, "password": settings.WFM_PASSWORD},
        timeout=settings.WFM_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    body = payload.get("data", payload) if isinstance(payload, dict) else {}
    token = body.get("accessToken") or body.get("access_token")
    if not token:
        raise RuntimeError("WFM login: accessToken topilmadi")
    return token


def _get_live(token: str):
    return requests.get(
        f"{_base()}/operators/live",
        headers={"Authorization": f"Bearer {token}"},
        timeout=settings.WFM_HTTP_TIMEOUT,
    )


def fetch_operator_snapshot():
    """
    WFM /operators/live -> {"data": {"agents": [...]}}.
    Xato bo'lsa bo'sh {"data": {"agents": []}} (attendance oqimi buzilmasin).
    """
    try:
        if not _token_cache["token"]:
            _token_cache["token"] = _login()

        resp = _get_live(_token_cache["token"])
        if resp.status_code == 401:  # token eskirgan -> bir marta qayta login
            _token_cache["token"] = _login()
            resp = _get_live(_token_cache["token"])
        resp.raise_for_status()

        payload = resp.json()
        body = payload.get("data", payload) if isinstance(payload, dict) else {}
        return {"data": {"agents": body.get("agents", []) or []}}
    except Exception:
        return {"data": {"agents": []}}
