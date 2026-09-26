#!/usr/bin/env bash
# Container-Watchdog (Runbook 9.6) — cAdvisor replacement.
# Writes container_up{name} 1/0 for every critical container into the
# node-exporter textfile collector (mounted at /textfile, see docker-compose.yml).
# Run every minute via cron as a user in the docker group:
#   * * * * * /srv/apps/observability/container-watchdog.sh
# Freshness: node_textfile_mtime_seconds tells Grafana if this script stopped
# running (alert "Container-Watchdog stale").
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

DIR="${WATCHDOG_TEXTFILE_DIR:-/home/luca/node-textfile}"
TMP="$DIR/.containers.prom.$$"

# critical containers that must be running
CRIT="traefik
LibreChat librechat-redis chat-mongodb chat-meilisearch vectordb rag_api
litellm postgres
fastapi-gateway fastapi-postgres pii-gate
code-interpreter-api
n8n
grafana prometheus loki promtail node-exporter"

{
  echo "# HELP container_up 1 if the container is running, 0 if not"
  echo "# TYPE container_up gauge"
  for c in $CRIT; do
    if [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ]; then
      v=1
    else
      v=0
    fi
    echo "container_up{name=\"$c\"} $v"
  done
} > "$TMP"

chmod 644 "$TMP"
mv "$TMP" "$DIR/containers.prom"
