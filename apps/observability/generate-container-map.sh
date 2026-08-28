#!/usr/bin/env bash
# Regenerates the Docker bridge-IP -> container-name mapping that Zeek
# uses to attribute conn.log/dns.log entries to a service (see
# specs/zeek-container-attribution.md). Zeek re-reads this file
# automatically (Input::REREAD) — no restart needed. Intended to run on
# a cron schedule (e.g. every 5 min); install via a crontab entry on the
# host, not tracked here.
#
# ponytail: writes to a tmpfile then mv's atomically, so if docker
# inspect fails partway (e.g. a container mid-redeploy), the previous
# valid mapping is left in place instead of a truncated one.
set -euo pipefail
trap 'ec=$?; echo "[$(date -Is)] generate-container-map.sh FAILED at line $LINENO (exit $ec) — previous mapping left untouched" >&2' ERR

OUT="/srv/apps/observability/container-map.tsv"
TMP="$(mktemp)"

CONTAINERS=(LibreChat rag_api fastapi-gateway litellm n8n pii-gate)

{
	printf '#fields\tip\tcontainer_name\n'
	printf '#types\taddr\tstring\n'
	for c in "${CONTAINERS[@]}"; do
		docker inspect "$c" --format \
			'{{range $net, $conf := .NetworkSettings.Networks}}{{$conf.IPAddress}}
{{end}}' | while IFS= read -r ip; do
			if [ -n "$ip" ]; then
				printf '%s\t%s\n' "$ip" "$c"
			fi
		done
	done
} > "$TMP"

mv "$TMP" "$OUT"
