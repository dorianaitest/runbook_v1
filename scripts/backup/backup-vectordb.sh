#!/usr/bin/env bash
# Daily pg_dump of the dedicated vectordb container's pgvector RAG database (LibreChat).
# Installed on the VPS at /srv/scripts/backup/backup-vectordb.sh, run via
# /etc/cron.d/backup-vectordb.
#
# Credential note: vectordb's POSTGRES_USER/POSTGRES_DB/POSTGRES_PASSWORD are read live from
# the running container (docker exec ... printenv) rather than from a host-side .env, since
# they are currently hardcoded literals in apps/librechat/docker-compose.yml (issue #22, out
# of scope here). This makes the script indifferent to whether #22 is ever fixed.
set -euo pipefail
umask 077  # dumps contain user data - keep the dir and files root-only

CONTAINER="vectordb"
BACKUP_DIR="/srv/backups/vectordb"
LOG="${BACKUP_DIR}/backup.log"
RETENTION_DAYS=14
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$BACKUP_DIR"

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1" >>"$LOG"
}

if ! CREDS="$(docker exec "$CONTAINER" printenv POSTGRES_USER POSTGRES_DB POSTGRES_PASSWORD)"; then
    log "FAIL could not read credentials from ${CONTAINER}"
    exit 1
fi
DB_USER="$(sed -n 1p <<<"$CREDS")"
DB="$(sed -n 2p <<<"$CREDS")"
DB_PASSWORD="$(sed -n 3p <<<"$CREDS")"

OUT="${BACKUP_DIR}/${DB}_${TIMESTAMP}.sql.gz"

if docker exec -e PGPASSWORD="$DB_PASSWORD" "$CONTAINER" \
    pg_dump -U "$DB_USER" -d "$DB" | gzip >"$OUT"; then
    log "OK ${OUT} ($(du -h "$OUT" | cut -f1))"
else
    STATUS=$?
    rm -f "$OUT"
    log "FAIL pg_dump exited ${STATUS}"
    exit "$STATUS"
fi

find "$BACKUP_DIR" -name '*.sql.gz' -mtime +"$RETENTION_DAYS" -delete
