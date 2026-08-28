import os
import logging

logger = logging.getLogger(__name__)

def load_secrets() -> None:
    vault_url = os.environ.get("AZURE_KEY_VAULT_URL")
    if not vault_url:
        logger.info("AZURE_KEY_VAULT_URL not set — using environment variables directly")
        return

    from azure.identity import ClientSecretCredential
    from azure.keyvault.secrets import SecretClient

    credential = ClientSecretCredential(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
    )
    client = SecretClient(vault_url=vault_url, credential=credential)

    for env_key, vault_key in {
        "DATABASE_URL":      "FASTAPI-DATABASE-URL",
        "AGENT_API_KEY":     "AGENT-API-KEY",
        "LITELLM_MASTER_KEY": "LITELLM-MASTER-KEY",
    }.items():
        os.environ[env_key] = client.get_secret(vault_key).value

    logger.info("Secrets loaded from Key Vault: %s", vault_url)
