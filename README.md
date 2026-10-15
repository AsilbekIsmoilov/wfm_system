# WC System Backend

Django backend for contact-center workforce accounting: schedules, working-hour debt, compensation and transfer requests, live attendance, and cycle archiving. Integrates with an external WFM platform, hourly-log API, and Google Sheets.

**Stack:** Python 3.12 · Django 5.2 · DRF · MySQL 8 · Redis 7 · Celery · Channels · Uvicorn · Nginx

## Applications & Services

| Application | Responsibility |
| --- | --- |
| `core` | Settings, HTTP/ASGI routing, Celery configuration, database router |
| `hourly_locks` | Operators, groups, shifts, schedules, debts, requests, policies, audit |
| `attendance` | Attendance processing, comments, WebSocket broadcasts, WFM history sync |
| `archive` | Historical snapshots, archived records, statistics; separate MySQL database |

Business services are organized under `hourly_locks/services/`:

| Area | Modules |
| --- | --- |
| Data ingestion & schedules | `external_api`, `log_loader`, `operator_sync`, `sheets_sync`, `shift`, `group_scope` |
| Debt accounting | `debt_calculator`, `work_debt`, `debt_compensation` |
| Compensation | `compensation_verifier`, `compensation_edit`, `otrabotka`, `otrabotka_verify`, `retroactive_check` |
| Transfers & exemptions | `transfer_verifier`, `transfer_edit`, `otprashivanie`, `ucheba`, `benefit`, `conflicts` |
| Administration & audit | `manual_adjustment`, `automation_override`, `event_log`, `action_log_format`, `notify` |
| Cycle management | `cycle`, `archive/{cycle,compensation,transfer,debt}` |

`hourly_locks/sync/` handles WFM authentication, HTTP requests, entity ingest, request apply/edit, audit ingest, and result reconciliation. `attendance/services/` contains `api`, `parser`, `schedule`, `attendance`, and `wfm_sync`; `archive/services/` contains `scope` and `statistics`.

## Infrastructure

| Compose service | Role |
| --- | --- |
| `nginx` | HTTP/WebSocket proxy on host port **4020**, static/media delivery |
| `web` | Django ASGI API via Uvicorn on internal port 8000 |
| `worker` | Executes Celery background tasks |
| `beat` | Schedules recurring Celery tasks; run one instance per deployment |
| `db` | MySQL operational (`default`) and historical (`archive`) databases |
| `redis` | Cache, task broker/results, and WebSocket channel layer |

Redis databases: **1** — Django cache; **3** — Celery broker; **4** — task results; **5** — Channels. Set `REDIS_URL` without a database suffix. Celery uses JSON serialization and **Asia/Tashkent (UTC+5)**.

| Scheduled task | Frequency |
| --- | --- |
| `attendance.tasks.attendance_worker` | Every 10 seconds |
| `hourly_locks.daily_pipeline` | Daily 09:00, processes yesterday |
| `attendance.tasks.push_attendance_to_wfm` | Daily 23:00 |
| `hourly_locks.auto_close_cycle` | Monthly, 20th at 23:30 |

The daily pipeline imports schedules/logs, calculates debts, verifies requests, pushes results to WFM, and checks cycle rollover. Accounting cycles span the **20th–19th**; automatic Sheets sync runs on the current month’s **2nd–22nd** days.

## Setup

Requires Docker Compose v2 and access to the external integrations.

```bash
cp .env.example .env                  # PowerShell: Copy-Item .env.example .env
```

1. Set unique `SECRET_KEY`, `DB_PASSWORD`, `SYNC_SERVICE_TOKEN`, and admin credentials. The sync token must match WFM.
2. Place the Google service-account key at `creds.json`, share the spreadsheets with its account, and change the credential mount in **web/worker/beat** to `./creds.json:/app/creds.json:ro`.
3. Add `creds.json` to `.dockerignore` before building.
4. Add `WFM_LOGIN`, `WFM_PASSWORD`, and `CSRF_TRUSTED_ORIGINS` to Compose’s `x-app-env` block and set them in `.env` (local origin: `http://localhost:4020`).

```bash
docker compose up -d --build
docker compose ps
```

The web entrypoint creates databases, applies both sets of migrations, seeds defaults, collects static files, and creates an admin if absent. Existing admin passwords are not overwritten.

| Configuration | Variables / policies |
| --- | --- |
| Django | `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` |
| MySQL | `DB_NAME`, `DB_NAME_ARCHIVE`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` |
| Redis/Celery | `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` |
| WFM | `WFM_BASE_URL`, `SYNC_SERVICE_TOKEN`, `WFM_HTTP_TIMEOUT`, `WFM_LOGIN`, `WFM_PASSWORD` |
| Bootstrap | `DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_PASSWORD` |
| Google credentials | `GOOGLE_CREDENTIAL_PATH` (default: `/app/creds.json` in Docker) |
| Hourly API policies | `external_api_url`, `external_api_timeout_seconds` |
| Sheets policies | `groups_sheet_id`, `groups_sheet_name`, `schedule_sheet_1_id`, `schedule_sheet_2_id` |

Policies are stored in `SystemPolicy`; schedule worksheets must be named `EXPORT`. Compose passes only declared environment variables; direct Python execution does not automatically load `.env`.

## API Reference

Base URL: **`http://localhost:4020`**. JWT: `Authorization: Bearer <access-token>`. Roles: `operator`, `supervisor`, `manager`, `admin`; access also depends on operator/group scope.

### Resources — `/api/v1/`

**CRUD:** collection `GET/POST`, detail `{id}/` `GET/PUT/PATCH/DELETE`. **Read:** collection and detail `GET`. All resource paths end in `/`; permissions apply per operation.

| Resource | Operations |
| --- | --- |
| `operators/` | CRUD |
| `groups/` | CRUD |
| `shifts/` | CRUD |
| `operator-schedule-days/` | CRUD |
| `cycles/` | Read |
| `request-type-rules/` | CRUD |
| `system-policies/` | CRUD |
| `work-logs-daily/` | Read |
| `work-debts/` | Read |
| `work-debt-details/` | Read |
| `compensations/` | CRUD |
| `transfers/` | CRUD |
| `manual-adjustments/` | CRUD |
| `automation-overrides/` | CRUD |
| `notes/` | CRUD |
| `event-logs/` | Read |

### Authentication & Actions — `/api/v1/`

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `auth/token/` | JWT pair; body: `username`, `password` |
| POST | `auth/token/refresh/` | Refresh access token; body: `refresh` |
| GET | `auth/me/` | Current user |
| GET | `cycles/active/` | Active cycle |
| POST | `cycles/{id}/close/` | Close/archive cycle |
| GET | `work-debts/light/` | Compact debt list |
| GET | `compensations/otrabotka/disabled-days/` | Unavailable repayment dates |
| POST | `compensations/otrabotka/preview/` | Preview repayment |
| POST | `compensations/otrabotka/` | Create repayment request |
| POST | `compensations/otrabotka/retroactive/` | Verify past repayment |
| POST | `compensations/debt/preview/` | Preview debt exemption |
| POST | `compensations/debt/` | Apply debt exemption |
| POST | `compensations/retroactive-check/` | Retroactive compensation check |
| POST | `transfers/otprashivanie/preview/`, `transfers/otprashivanie/` | Preview/create time-off request |
| POST | `transfers/ucheba/preview/`, `transfers/ucheba/` | Preview/create training request |
| POST | `transfers/benefit/preview/`, `transfers/benefit/` | Preview/create exemption |
| POST | `manual-adjustments/{id}/approve/` | Approve adjustment |
| POST | `automation-overrides/{id}/deactivate/` | Disable override |
| GET/POST | `run-daily-pipeline/` | Run pipeline; `date`, `skip_sheets`, `full`, `sync` |
| GET | `schema/`, `docs/swagger/`, `docs/redoc/` | OpenAPI and interactive documentation |

Pipeline execution defaults to Celery (`202`, `task_id`); `sync=true` runs inline. Requires manager/admin access or the supported query service token. JWT access/refresh lifetimes: **2 hours / 14 days**.

### Integration & Attendance — absolute paths

Sync endpoints use **`x-sync-token`** and have no trailing slash.

| Method | Path |
| --- | --- |
| POST | `/api/sync/apply-request` |
| PATCH/POST | `/api/sync/edit-compensation`, `/api/sync/edit-transfer` |
| POST | `/api/sync/upsert/{entity}`, `/api/sync/delete/{entity}` |
| POST | `/api/sync/action-log` |
| GET | `/api/action-logs` (also `/api/action-logs/`) |
| GET | `/api/attendance/v1/attendance-test/`, `/api/attendance/v1/board/` |
| POST | `/api/attendance/v1/comment/` |
| WebSocket | `/ws/attendance/` |
| Browser | `/admin/`, `/api-auth/login/`, `/api-auth/logout/` |

Sync entities: `operator-groups`, `shifts`, `type-rules`, `cycles`, `operators`, `work-debts`, `work-debt-details`, `compensations`, `transfers`, `compensation-debt-links`.

**Archive API is implemented but not mounted in `core/urls.py`.** Its declared prefix is `/api/v1/archive/`, with read-only `cycles`, `operator-snapshots`, `work-debts`, `work-debt-details`, `work-logs-daily`, `compensations`, `transfers`, `manual-adjustments`, `event-logs` collection/detail routes and `statistics/`.

## Operations

```bash
docker compose logs -f worker
docker compose exec worker celery -A core inspect ping
docker compose exec worker celery -A core call hourly_locks.daily_pipeline
docker compose exec web python manage.py check
docker compose exec web python manage.py run_daily_pipeline --date 2026-10-13
docker compose exec web python manage.py sync_schedules
docker compose exec web python manage.py recompute_work_debts --cycle 12
docker compose exec web python manage.py changepassword <username>
```

Use production secrets, `DEBUG=0`, explicit hosts/origins, HTTPS, and restricted CORS. Keep credentials out of Git/images. Back up both databases: **`docker compose down -v` deletes persistent data**. The attendance WebSocket currently has no login check; pipeline completion may include failed steps. Business-rule test coverage and GitHub Actions are not yet provided.
