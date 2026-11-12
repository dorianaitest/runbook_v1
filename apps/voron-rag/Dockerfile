FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH=/app/.venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# Editable install: the code finds its data at /app/data (paths are relative to src/).
RUN uv sync --frozen --no-dev
# Outside data/ in the repo because runbook_v1's .gitignore excludes every data/ directory.
COPY voron24_cases.jsonl data/raw/
COPY start.sh ./

CMD ["sh", "start.sh"]
