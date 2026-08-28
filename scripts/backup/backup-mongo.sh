#!/usr/bin/env bash
# Daily mongodump of chat-mongodb's LibreChat database.
# Installed on the VPS at /srv/scripts/backup/backup-mongo.sh, run via /etc/cron.d/backup-mongo.
set -euo pipefail
umask 077  # dumps contain chat messages/user data - keep the dir and files root-only

CONTAINER="chat-mongodb"
DB="LibreChat"
BACKUP_DIR="/srv/backups/mongodb"
LOG="${BACKUP_DIR}/backup.log"
RETENTION_DAYS=14
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${BACKUP_DIR}/${CONTAINER}_${TIMESTAMP}.archive.gz"

mkdir -p "$BACKUP_DIR"

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1" >>"$LOG"
}

if docker exec "$CONTAINER" mongodump --db="$DB" --archive --gzip >"$OUT"; then
    log "OK ${OUT} ($(du -h "$OUT" | cut -f1))"
else
    STATUS=$?
    rm -f "$OUT"
    log "FAIL mongodump exited ${STATUS}"
    exit "$STATUS"
fi

find "$BACKUP_DIR" -name '*.archive.gz' -mtime +"$RETENTION_DAYS" -delete
