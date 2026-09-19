#!/bin/bash
# Fetches secrets from Azure Key Vault and exports them as environment variables.
# Usage: bash -c 'set -a && source /path/to/.env && set +a && source /srv/load-secrets.sh && docker compose -f <file> up -d'
#
# set -euo pipefail is guarded so sourcing this script does not kill the
# parent shell if a command fails. Errors are still surfaced via return codes.
[[ "${BASH_SOURCE[0]}" == "${0}" ]] && set -euo pipefail

# ── Bootstrap credentials (the only secrets that live in .env) ───────────────
: "${AZURE_TENANT_ID:?Missing AZURE_TENANT_ID in environment}"
: "${AZURE_CLIENT_ID:?Missing AZURE_CLIENT_ID in environment}"
: "${AZURE_CLIENT_SECRET:?Missing AZURE_CLIENT_SECRET in environment}"
: "${AZURE_KEY_VAULT_URL:?Missing AZURE_KEY_VAULT_URL in environment}"

# Extract vault name from URL (https://my-vault.vault.azure.net/ -> my-vault)
VAULT_NAME="${AZURE_KEY_VAULT_URL#https://}"
VAULT_NAME="${VAULT_NAME%%.*}"

# ── Authenticate ─────────────────────────────────────────────────────────────
az login \
    --service-principal \
    --tenant   "$AZURE_TENANT_ID" \
    --username "$AZURE_CLIENT_ID" \
    --password "$AZURE_CLIENT_SECRET" \
    --allow-no-subscriptions \
    --output none 2>/dev/null

fetch() {
    az keyvault secret show \
        --vault-name "$VAULT_NAME" \
        --name "$1" \
        --query value \
        --output tsv
}

# ── PostgreSQL ────────────────────────────────────────────────────────────────
export POSTGRES_PASSWORD=$(fetch POSTGRES-SUPERUSER-PASSWORD)
export FASTAPI_POSTGRES_PASSWORD=$(fetch FASTAPI-POSTGRES-PASSWORD)
export FASTAPI_DATABASE_URL=$(fetch FASTAPI-DATABASE-URL)
# Per-DB users on the shared postgres (created by infra/postgres/init/*.sh)
export APPDB_PASSWORD=$(fetch APPDB-PASSWORD)

# ── LiteLLM / Azure OpenAI ───────────────────────────────────────────────────
export LITELLM_MASTER_KEY=$(fetch LITELLM-MASTER-KEY)
export LITELLM_DATABASE_URL=$(fetch LITELLM-DATABASE-URL)
export LITELLM_POSTGRES_PASSWORD=$(fetch LITELLM-POSTGRES-PASSWORD)
export AZURE_API_KEY=$(fetch AZURE-OPENAI-API-KEY)
export LANGFUSE_PUBLIC_KEY=$(fetch LANGFUSE-PUBLIC-KEY)
export LANGFUSE_SECRET_KEY=$(fetch LANGFUSE-SECRET-KEY)

# ── AWS Bedrock (Claude via LiteLLM) ─────────────────────────────────────────
export AWS_ACCESS_KEY_ID=$(fetch AWS-ACCESS-KEY-ID)
export AWS_SECRET_ACCESS_KEY=$(fetch AWS-SECRET-ACCESS-KEY)

# ── FastAPI ───────────────────────────────────────────────────────────────────
export AGENT_API_KEY=$(fetch AGENT-API-KEY)

# ── n8n ───────────────────────────────────────────────────────────────────────
export N8N_POSTGRES_PASSWORD=$(fetch N8N-POSTGRES-PASSWORD)
export N8N_ENCRYPTION_KEY=$(fetch N8N-ENCRYPTION-KEY)

# ── Observability ─────────────────────────────────────────────────────────────
export GRAFANA_ADMIN_PASSWORD=$(fetch GRAFANA-ADMIN-PASSWORD)

# ── Revoke session ────────────────────────────────────────────────────────────
az logout --output none 2>/dev/null

echo "Secrets loaded from vault: $VAULT_NAME"
