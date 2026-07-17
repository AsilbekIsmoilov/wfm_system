"""
Человекочитаемое (RU) представление RequestActionLog для админки и API.

payload зависит от метода:
  POST   — тело создания; PATCH — {before, after}; DELETE — полный снимок;
  GET    — {id} или {query}.
Всё best-effort: любые отсутствующие/чужие поля просто пропускаются, никогда
не падает.
"""
from datetime import timedelta

from django.utils import timezone


def _fmt_date(s):
    """'2026-07-16' | '2026-07-16T..' -> '16.07.2026'."""
    if not s:
        return None
    s = str(s)[:10]
    parts = s.split("-")
    if len(parts) == 3:
        return f"{parts[2]}.{parts[1]}.{parts[0]}"
    return s


def _fmt_days(days):
    if not isinstance(days, (list, tuple)) or not days:
        return None
    return ", ".join(d for d in (_fmt_date(x) for x in days) if d)


def _parse_hms(v):
    """'HH:MM:SS' | 'H:MM' | минуты(int) -> timedelta."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return timedelta(minutes=int(v))
    s = str(v).strip()
    if ":" in s:
        p = [int(x) for x in s.split(":")]
        while len(p) < 3:
            p.append(0)
        return timedelta(hours=p[0], minutes=p[1], seconds=p[2])
    try:
        return timedelta(seconds=int(s))
    except ValueError:
        return None


def _hms(td):
    if td is None:
        return "—"
    total = int(td.total_seconds())
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _fmt_durations(durs):
    if not isinstance(durs, (list, tuple)) or not durs:
        return None
    total = timedelta(0)
    parsed = []
    for v in durs:
        td = _parse_hms(v)
        if td is not None:
            total += td
        parsed.append(str(v))
    line = ", ".join(parsed)
    if len(parsed) > 1:
        line += f"  (всего {_hms(total)})"
    return line


def _type_name(payload, entity_external_id):
    """type_rule display_name: из payload.type_rule_id (int) или из самой заявки."""
    from hourly_locks.models import RequestTypeRule, Compensation, Transfer
    tid = (payload or {}).get("type_rule_id")
    if isinstance(tid, int):
        name = RequestTypeRule.objects.filter(id=tid).values_list(
            "display_name", flat=True).first()
        if name:
            return name
    # POST: type_rule_id — это Mongo _id (строка); пробуем через существующую заявку
    if entity_external_id:
        for Model in (Compensation, Transfer):
            obj = Model.objects.filter(id=entity_external_id).select_related(
                "type_rule").first()
            if obj and obj.type_rule:
                return obj.type_rule.display_name
    return None


def _operator_label(payload):
    """operator_id из снимка (int external_id) -> ФИО; иначе None."""
    from hourly_locks.models import Operator
    oid = (payload or {}).get("operator_id")
    if isinstance(oid, int):
        op = Operator.objects.filter(id=oid).first()
        if op:
            return f"{op.surname} {op.name}".strip() or f"#{oid}"
        return f"оператор #{oid}"
    return None


def _entity_label(log):
    """'заявку #23 «Отработка»' (лучшее, что удаётся разрешить)."""
    ent = "заявку"
    if log.entity_external_id:
        ent += f" #{log.entity_external_id}"
    payload = log.payload or {}
    snap = payload if log.method in ("POST", "DELETE") else payload.get("before") or {}
    name = _type_name(snap or payload, log.entity_external_id)
    if name:
        ent += f" «{name}»"
    return ent


def _operator_fullname(payload, entity_external_id, entity_type):
    """ФИО оператора: из снимка (operator_id=int) или из самой заявки (POST)."""
    from hourly_locks.models import Operator, Compensation, Transfer
    oid = (payload or {}).get("operator_id")
    if isinstance(oid, int):
        op = Operator.objects.filter(id=oid).first()
        if op:
            return op.full_name
    if entity_external_id:
        Model = Transfer if entity_type == "transfer" else Compensation
        obj = Model.objects.filter(id=entity_external_id).select_related("operator").first()
        if obj and obj.operator:
            return obj.operator.full_name
    return None


def _status(payload, entity_external_id, entity_type):
    """Статус: из снимка (payload.status) или из самой заявки (POST)."""
    from hourly_locks.models import Compensation, Transfer
    st = (payload or {}).get("status")
    if st:
        return st
    if entity_external_id:
        Model = Transfer if entity_type == "transfer" else Compensation
        st = Model.objects.filter(id=entity_external_id).values_list(
            "status", flat=True).first()
        if st:
            return st
    return None


def _row(label, value):
    """Одна строка: метка + значение. Всё по левому краю, значения выровнены
    в один столбец (одинаковый отступ для ВСЕХ строк)."""
    return f"{(label + ':').ljust(14)}{value}"


# Всё в один уровень — мета и тело одинаково.
_meta_row = _row


# Поля, изменения которых показываем в PATCH-диффе (человеко-понятные).
_DIFF_FIELDS = [
    ("days", "Дни"), ("durations", "Длительность"),
    ("applied_duration", "Списано"), ("comment", "Комментарий"),
    ("status", "Статус"), ("planned_date", "Дата"),
]


def _diff_lines(before, after):
    lines = []
    for key, label in _DIFF_FIELDS:
        if key not in after:
            continue
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        lines.append(_row(label, f"{_val(key, old)} → {_val(key, new)}"))
    return lines


def _val(key, v):
    if v is None:
        return "—"
    if key == "days":
        return _fmt_days(v) or "—"
    if key == "durations":
        return ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)
    if key == "planned_date":
        return _fmt_date(v) or str(v)
    return f"\"{v}\"" if isinstance(v, str) else str(v)


def _snap(log):
    """Релевантный снимок: POST/DELETE — payload; PATCH — before."""
    p = log.payload or {}
    return p.get("before") or {} if log.method == "PATCH" else p


_VERB = {"POST": "создал", "PATCH": "изменил", "DELETE": "удалил", "GET": "просмотрел"}


def _req_id(log):
    """ID заявки в WFM: Mongo _id самой заявки, иначе external_id."""
    return log.entity_wfm_id or (
        str(log.entity_external_id) if log.entity_external_id else "—")


def _meta_lines(log):
    """ID / Тип / Оператор."""
    snap = _snap(log)
    lines = [_row("ID", _req_id(log))]
    tname = _type_name(snap, log.entity_external_id)
    if tname:
        lines.append(_meta_row("Тип", f"«{tname}»"))
    opname = _operator_fullname(snap, log.entity_external_id, log.entity_type)
    if opname:
        lines.append(_meta_row("Оператор", opname))
    return lines


def _detail_lines(log, body):
    """Дни / Длительность / Статус / Комментарий (тело или снимок)."""
    lines = []
    days = _fmt_days(body.get("days"))
    if days:
        lines.append(_row("Дни", days))
    durs = _fmt_durations(body.get("durations"))
    if durs:
        lines.append(_row("Длительность", durs))
    if body.get("applied_duration"):
        lines.append(_row("Списано", body["applied_duration"]))
    st = _status(body, log.entity_external_id, log.entity_type)
    if st:
        lines.append(_row("Статус", st))
    if body.get("comment"):
        lines.append(_row("Комментарий", f"\"{body['comment']}\""))
    return lines


def format_action_log(log) -> str:
    """Многострочный человекочитаемый (RU) текст лога."""
    try:
        who = log.user_name or log.user_id or "?"
        verb = _VERB.get(log.method, log.method)
        when = (timezone.localtime(log.created_at).strftime("%d.%m.%Y %H:%M")
                if log.created_at else "—")
        result = "Успешно" if log.success else "❌ Ошибка"
        result_line = _row("Результат", f"{result} ({log.status_code}) · {when}")
        payload = log.payload or {}

        head = [f"{who} — {verb} заявку", *_meta_lines(log)]

        if log.method == "PATCH":
            diff = _diff_lines(payload.get("before") or {}, payload.get("after") or {})
            body = ["Изменено:", *(diff or ["(изменений полей нет)"])]
            return "\n".join([*head, *body, result_line])

        if log.method == "GET" and not log.entity_external_id:
            q = payload.get("query") or {}
            filt = ", ".join(f"{k}={v}" for k, v in q.items()) or "без фильтра"
            return "\n".join([
                f"{who} — просмотрел список ({log.entity_type}: {filt})",
                result_line,
            ])

        # POST / DELETE / GET(detail)
        return "\n".join([*head, *_detail_lines(log, payload), result_line])
    except Exception as exc:  # никогда не падаем в админке/API
        return f"[ошибка форматирования: {exc}]"
