#!/usr/bin/env bash
# Deploy /opt/tubenotes-ai without creating a second PostgreSQL database.
#
# Required once per deploy:
#   CONFIRM_SHARED_DB_BACKUP=yes bash deploy/deploy-shared-db.sh
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="docker compose -p tubenotes-ai -f docker-compose.shared-db.prod.yml"

if [[ "${CONFIRM_SHARED_DB_BACKUP:-}" != "yes" ]]; then
  echo "Refusing to start: first take a PostgreSQL backup of the shared database."
  echo "After the backup succeeds, run:"
  echo "  CONFIRM_SHARED_DB_BACKUP=yes bash deploy/deploy-shared-db.sh"
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "ERROR: .env nahi mila. cp .env.prod.example .env karke values bharo."
  exit 1
fi

for key in SECRET_KEY DEVICE_HASH_SECRET POSTGRES_PASSWORD; do
  val="$(grep -E "^${key}=" .env | cut -d= -f2- || true)"
  if [[ -z "${val}" || "${val}" == *"<FILL"* || "${val}" == *"change-me"* ]]; then
    echo "ERROR: .env mein ${key} valid nahi hai."
    exit 1
  fi
done

echo "==> Building and starting TubeNotes.ai on 127.0.0.1:8011"
$COMPOSE up -d --build

echo -n "==> health check "
for i in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8011/healthz >/dev/null 2>&1; then
    echo "OK"
    curl -s http://127.0.0.1:8011/healthz; echo
    exit 0
  fi
  echo -n "."
  sleep 2
done

echo " FAILED"
$COMPOSE logs --tail=80 api
exit 1
