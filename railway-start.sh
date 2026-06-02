#!/usr/bin/env bash
# Railway role dispatcher: one image, process selected by $SERVICE_ROLE.
# Invoked by the Docker CMD after entrypoint.sh sets up tini + PulseAudio.
set -euo pipefail
ROLE="${SERVICE_ROLE:-web}"
echo "[railway-start] SERVICE_ROLE=${ROLE}"
case "$ROLE" in
  web)
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput || true
    exec gunicorn attendee.wsgi:application \
      --bind "0.0.0.0:${PORT:-8000}" \
      --workers "${WEB_CONCURRENCY:-2}" \
      --timeout "${WEB_TIMEOUT:-120}" \
      --log-file -
    ;;
  worker)
    exec celery -A attendee worker -l info \
      --concurrency="${CELERY_CONCURRENCY:-1}" \
      --max-memory-per-child="${CELERY_MAX_MEMORY:-3500000}"
    ;;
  beat)
    exec celery -A attendee beat -l info
    ;;
  *)
    echo "[railway-start] Unknown SERVICE_ROLE='${ROLE}' (expected web|worker|beat)" >&2
    exit 1
    ;;
esac
