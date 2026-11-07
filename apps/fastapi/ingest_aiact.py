"""Einmal-Ingestion des EU-AI-Act in pgvector. Liest data/aiact_chunks.jsonl,
embeddet ueber LiteLLM und schreibt in die Tabelle aiact_chunks.

Im Container ausfuehren:
    docker exec fastapi-gateway python ingest_aiact.py

Idempotent: TRUNCATE + Neuaufbau bei jedem Lauf.
"""
import asyncio
import json
from pathlib import Path

from vault import load_secrets
from database import get_pool
from aiact_rag import embed

BATCH = 100


async def main():
    load_secrets()  # LITELLM_MASTER_KEY + DATABASE_URL in Env
    rows = [
        json.loads(l)
        for l in (Path(__file__).parent / "aiact_chunks.jsonl").open(encoding="utf-8")
    ]
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS aiact_chunks (
                   id TEXT PRIMARY KEY,
                   section TEXT,
                   content TEXT,
                   embedding vector(1536)
               );"""
        )
        await conn.execute("TRUNCATE aiact_chunks;")

    total = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        embs = await embed([r["text"] for r in batch])
        recs = [(r["id"], r["section"], r["text"], str(e)) for r, e in zip(batch, embs)]
        async with pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO aiact_chunks (id, section, content, embedding)
                   VALUES ($1, $2, $3, $4::vector)""",
                recs,
            )
        total += len(batch)
        print(f"ingested {total}/{len(rows)}")

    # Bewusst KEIN ivfflat-Index: bei ~580 Zeilen ist der exakte Seq-Scan
    # sub-ms und liefert echte Nearest-Neighbors; ein ivfflat mit lists=100
    # wuerde bei einer Probe nur ~1/100 der Daten absuchen und die Trefferqualitaet
    # verschlechtern. Bei deutlich mehr Dokumenten spaeter einen HNSW-Index setzen.
    print("done:", total, "chunks")


if __name__ == "__main__":
    asyncio.run(main())
