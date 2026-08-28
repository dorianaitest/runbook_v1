#!/bin/bash
# Daily backup for the shared `postgres` container (pg17): appdb, litellm, n8n databases,
# plus the n8n encryption key (single protected snapshot) and n8n binary-data directory.
# See specs/backup-postgres-shared.md for the full design/rationale.
set -euo pipefail
umask 077  # dumps contain user data - keep the dir and files root-only

BACKUP_DIR="/srv/backups/postgres-shared"
LOG_FILE="$BACKUP_DIR/backup.log"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RETENTION_DAYS=14
DATABASES=(appdb litellm n8n)
BINARY_DATA_DIR="/srv/data/n8n/binaryData"
KEY_FILE="$BACKUP_DIR/n8n_encryption_key.enc"

mkdir -p "$BACKUP_DIR"

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1" >> "$LOG_FILE"
}

on_error() {
    log "FAILED (exit $?) at line $1"
}
trap 'on_error $LINENO' ERR

# ── Credentials: real .env files on the VPS (Key Vault is not live yet) ──────
set -a
source /srv/infra/postgres/.env
source /srv/apps/n8n/.env
set +a

# ── 1. Per-database SQL dumps ─────────────────────────────────────────────────
for db in "${DATABASES[@]}"; do
    dump_file="$BACKUP_DIR/${db}_${TIMESTAMP}.sql.gz"
    docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
        pg_dump -U appuser -d "$db" | gzip > "$dump_file"
    log "dumped $db -> $dump_file"
done

# ── 2. n8n encryption key: single snapshot, only rewritten on change ─────────
new_hash="$(printf '%s' "$N8N_ENCRYPTION_KEY" | sha256sum | cut -d' ' -f1)"
old_hash=""
[[ -f "$KEY_FILE" ]] && old_hash="$(sha256sum "$KEY_FILE" | cut -d' ' -f1)"

if [[ "$new_hash" != "$old_hash" ]]; then
    printf '%s' "$N8N_ENCRYPTION_KEY" > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
    log "n8n encryption key snapshot updated (key changed since last run)"
else
    log "n8n encryption key snapshot unchanged, skipped"
fi

# ── 3. n8n binary data directory (filesystem mode, appears lazily) ───────────
if [[ -d "$BINARY_DATA_DIR" ]]; then
    tar_file="$BACKUP_DIR/n8n_binary_data_${TIMESTAMP}.tar.gz"
    tar czf "$tar_file" -C "$(dirname "$BINARY_DATA_DIR")" "$(basename "$BINARY_DATA_DIR")"
    log "archived binary data -> $tar_file"
else
    log "binary data dir not present, skipped"
fi

# ── 4. Retention: delete dated artifacts older than 14 days (never the key file) ──
find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name '*_[0-9]*.sql.gz' -o -name 'n8n_binary_data_*.tar.gz' \) \
    -mtime "+$RETENTION_DAYS" -delete

log "backup run complete"
