#!/bin/bash
# Runs once, on first init of an empty data dir (docker-entrypoint-initdb.d).
# Creates the per-service users + databases on the shared postgres (F-03,
# least privilege: each service owns only its own DB).
#
# Passwords are injected via docker-compose 'environment:' interpolation,
# which in turn is populated by load-secrets.sh (Azure Key Vault). They never
# touch disk. Passwords are passed as psql -v variables and referenced with
# :'var', so psql handles the quoting/escaping safely.
set -euo pipefail

: "${LITELLM_POSTGRES_PASSWORD:?missing LITELLM_POSTGRES_PASSWORD}"
: "${N8N_POSTGRES_PASSWORD:?missing N8N_POSTGRES_PASSWORD}"
: "${APPDB_PASSWORD:?missing APPDB_PASSWORD}"

psql -v ON_ERROR_STOP=1 \
     -v litellm_pw="$LITELLM_POSTGRES_PASSWORD" \
     -v n8n_pw="$N8N_POSTGRES_PASSWORD" \
     -v appdb_pw="$APPDB_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-'EOSQL'
	CREATE USER litellm WITH PASSWORD :'litellm_pw';
	CREATE DATABASE litellm OWNER litellm;

	CREATE USER n8n WITH PASSWORD :'n8n_pw';
	CREATE DATABASE n8n OWNER n8n;

	CREATE USER appdb WITH PASSWORD :'appdb_pw';
	CREATE DATABASE appdb OWNER appdb;
EOSQL

echo "init: created databases litellm, n8n, appdb"
