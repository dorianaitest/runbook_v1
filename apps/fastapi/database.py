import asyncpg
import os

_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        url = os.getenv("DATABASE_URL", "postgresql://fastapi:fastapi@fastapi-postgres:5432/fastapi")
        _pool = await asyncpg.create_pool(url)
    return _pool

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS request_logs (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                model TEXT,
                prompt_tokens INT DEFAULT 0,
                completion_tokens INT DEFAULT 0,
                total_tokens INT GENERATED ALWAYS AS (prompt_tokens + completion_tokens) STORED,
                status_code INT,
                duration_ms INT
            );
        """)
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                source TEXT,
                chunk_index INT DEFAULT 0,
                content TEXT,
                embedding vector(1536)
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS documents_embedding_idx
            ON documents USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100);
        """)

async def log_request(model: str, prompt_tokens: int, completion_tokens: int, status_code: int, duration_ms: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO request_logs (model, prompt_tokens, completion_tokens, status_code, duration_ms)
               VALUES ($1, $2, $3, $4, $5)""",
            model, prompt_tokens, completion_tokens, status_code, duration_ms
        )

async def get_stats():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                model,
                COUNT(*) AS requests,
                SUM(total_tokens) AS total_tokens,
                AVG(duration_ms) AS avg_duration_ms,
                DATE_TRUNC('day', created_at) AS day
            FROM request_logs
            WHERE created_at > NOW() - INTERVAL '30 days'
            GROUP BY model, day
            ORDER BY day DESC, requests DESC;
        """)
        return [dict(r) for r in rows]