#!/usr/bin/env bash
# Daily pg_dump of the dedicated fastapi-postgres container's "fastapi" database.
# Installed on the VPS at /srv/scripts/backup/backup-fastapi-postgres.sh, run via
# /etc/cron.d/backup-fastapi-postgres.
#
# Credential note: Key Vault (/srv/load-secrets.sh) is not wired up yet, so this sources the
# same .env the app itself is currently started with (POSTGRES_PASSWORD in
# /srv/apps/fastapi/.env), matching apps/fastapi/.env.example. Revisit once Key Vault is live.
set -euo pipefail
umask 077  # dumps contain user data - keep the dir and files root-only

CONTAINER="fastapi-postgres"
DB="fastapi"
DB_USER="fastapi"
ENV_FILE="/srv/apps/fastapi/.env"
BACKUP_DIR="/srv/backups/fastapi-postgres"
LOG="${BACKUP_DIR}/backup.log"
RETENTION_DAYS=14
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${BACKUP_DIR}/${DB}_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1" >>"$LOG"
}

set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD not set in ${ENV_FILE}}"

if docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER" \
    pg_dump -U "$DB_USER" -d "$DB" | gzip >"$OUT"; then
    log "OK ${OUT} ($(du -h "$OUT" | cut -f1))"
else
    STATUS=$?
    rm -f "$OUT"
    log "FAIL pg_dump exited ${STATUS}"
    exit "$STATUS"
fi

find "$BACKUP_DIR" -name '*.sql.gz' -mtime +"$RETENTION_DAYS" -delete
