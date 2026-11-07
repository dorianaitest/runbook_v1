"""Retrieval fuer das EU-AI-Act-Grounding: Embeddings ueber LiteLLM
(text-embedding-3-small, budget-getrackt) + Vektorsuche in pgvector
(Tabelle aiact_chunks in der fastapi-Postgres)."""
import contextvars
import os
import httpx
from database import get_pool

LITELLM_BASE_URL = "http://litellm:4000"
EMBED_MODEL = os.getenv("AIACT_EMBED_MODEL", "text-embedding-3-small")

# Key fuer die LiteLLM-Aufrufe. Zur Laufzeit setzt der Gateway hier den
# Authorization-Bearer der eingehenden LibreChat-Anfrage (= sk-librechat-admin),
# damit die Embeddings denselben budget-getrackten Key nutzen wie der Chat.
# Fallback auf LITELLM_MASTER_KEY (z. B. beim Ingest via `docker exec -e`).
request_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_key", default=None
)


def current_key() -> str:
    return request_key.get() or os.getenv("LITELLM_MASTER_KEY") or "sk-dummy"


async def embed(texts: list[str]) -> list[list[float]]:
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(
            f"{LITELLM_BASE_URL}/v1/embeddings",
            headers={"Authorization": f"Bearer {current_key()}"},
            json={"model": EMBED_MODEL, "input": texts},
        )
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]


async def retrieve(query: str, k: int = 6) -> list[dict]:
    """Top-k Textstellen nach Kosinus-Aehnlichkeit. Gibt section/content/score."""
    vec = (await embed([query]))[0]
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT section, content, 1 - (embedding <=> $1::vector) AS score
               FROM aiact_chunks
               ORDER BY embedding <=> $1::vector
               LIMIT $2""",
            str(vec), k,
        )
    return [dict(r) for r in rows]
