#!/bin/sh
set -e

if [ "$RUN_MIGRATIONS" = "1" ]; then
  # БД создаём здесь (у web есть доступ к MySQL). НЕ полагаемся на
  # init-databases.sql: bind-mount ненадёжен на CI-раннере (docker-демон хоста
  # не видит файл по пути checkout'а), поэтому default+archive БД могли не
  # создаться. Идемпотентно (CREATE DATABASE IF NOT EXISTS).
  echo "[entrypoint] Ensuring databases exist..."
  python - <<'PYEOF'
import os, MySQLdb
conn = MySQLdb.connect(
    host=os.environ.get("DB_HOST", "db"),
    user=os.environ.get("DB_USER", "root"),
    passwd=os.environ.get("DB_PASSWORD", ""),
    port=int(os.environ.get("DB_PORT", "3306")),
)
cur = conn.cursor()
for db in (os.environ.get("DB_NAME", "wc_system_default"),
           os.environ.get("DB_NAME_ARCHIVE", "wc_system_archive")):
    cur.execute(
        "CREATE DATABASE IF NOT EXISTS `%s` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" % db
    )
    print("[entrypoint] DB ensured:", db)
conn.commit()
PYEOF

  echo "[entrypoint] Applying migrations (default)..."
  python manage.py migrate --noinput
  echo "[entrypoint] Applying migrations (archive)..."
  python manage.py migrate --database=archive --noinput

  echo "[entrypoint] Seeding initial data (idempotent)..."
  python manage.py seed_initial_data

  echo "[entrypoint] Collecting static files..."
  python manage.py collectstatic --noinput

  echo "[entrypoint] Ensuring admin superuser..."
  DJANGO_SUPERUSER_USERNAME="${DJANGO_SUPERUSER_USERNAME:-admin}" \
  DJANGO_SUPERUSER_PASSWORD="${DJANGO_SUPERUSER_PASSWORD:-123}" \
  python manage.py shell -c "import os; from django.contrib.auth import get_user_model; U = get_user_model(); un = os.environ.get('DJANGO_SUPERUSER_USERNAME', 'admin'); pw = os.environ.get('DJANGO_SUPERUSER_PASSWORD', '123'); u, created = U.objects.get_or_create(username=un, defaults={'is_staff': True, 'is_superuser': True, 'role': 'admin'}); (u.set_password(pw), u.save(), print(f'[entrypoint] superuser {un} created')) if created else print(f'[entrypoint] superuser {un} already exists')"
fi

exec "$@"
