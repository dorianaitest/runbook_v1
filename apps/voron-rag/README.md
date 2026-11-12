# voron-rag

A local, read-only troubleshooting assistant for Voron 2.4 / 2.4r2 printers running Klipper.
It answers from official Voron/Klipper documentation and 11 curated community cases, cites every claim, and says so when the evidence is missing.
It never controls a printer.

Built as a guided learning project; each milestone has a doc in `docs/milestones/` and a git tag (`m1` ... `m10`).

## How it works

```
question -> hybrid retrieval (dense + BM25, RRF, top 6) -> labeled evidence (~3,000 tokens) -> ONE chat call -> citation check
```

- Deterministic 2-step RAG: no agents, loops or retries; one query embedding and one chat call per question.
- Evidence keeps authority levels apart: official documentation, community case (with resolution status), knowledge-base guidance (`[Cxx-note]`, our curator notes).
- The model cites IDs only; URLs come from metadata, so links cannot be invented.
- Abstains with `INSUFFICIENT EVIDENCE:` for off-topic or unsupported questions.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and an OpenAI API key.

```bash
uv sync
cp .env.example .env          # then put your key in .env (git-ignored); never commit it
uv run voron-rag config       # shows models and whether the key is set (never the key)
uv run voron-rag fetch-docs   # downloads 11 GPL-3.0 docs at pinned commits into data/raw/docs/ (git-ignored)
uv run voron-rag index        # hybrid collection; about $0.001; downloads the local BM25 model once
uv run voron-rag index --no-hybrid   # dense-only collection, used by `semantic` and `compare`
```

`.env` keys: `OPENAI_API_KEY`, optional `OPENAI_CHAT_MODEL` (default `gpt-6-luna`), optional `OPENAI_EMBEDDING_MODEL` (default `text-embedding-3-small`).

## Use

```bash
uv run voron-rag ask "MCU 'mcu' shutdown: ADC out of range"         # about $0.0004
uv run voron-rag ask "zero_reference_position" --debug              # also prints the exact evidence sent
uv run voron-rag search '"Lost communication with MCU"'             # offline keyword search, free
```

| Command | Cost | Purpose |
| --- | --- | --- |
| `cases`, `search`, `config`, `index --dry-run` | free, offline | inspect data and configuration |
| `fetch-docs` | free, network | download approved docs |
| `index` | only changed chunks are embedded | sync the Qdrant index (content-hash incremental) |
| `semantic`, `compare` | 1 query embedding each | dense-only retrieval experiments |
| `ask` | 1 embedding + 1 chat call | grounded answer |
| `eval-retrieval [--retriever keyword\|semantic\|hybrid]` | keyword free; else ~22 embeddings | Hit@1, Recall@k, MRR |
| `eval-answers [--repeat N]` | ~$0.019 per run of 51 questions | answers saved to `data/eval/results/` for manual grading |

## Chat UI (local LibreChat)

`voron-rag serve` exposes the same pipeline as an OpenAI-compatible API; a local LibreChat container uses it as the "Voron RAG" endpoint (spec: `docs/spec/librechat-local-spec.md`).
Requires Docker (Docker Desktop with WSL integration on Windows) and `VORON_RAG_API_KEY` (any random string, not your OpenAI key) in `.env`.

```bash
bash librechat/make-env.sh                         # once: LibreChat secrets + the API key into librechat/.env
docker compose -f librechat/docker-compose.yml up -d
uv run voron-rag serve                             # keep running; then open http://localhost:3080, register, pick Voron RAG
```

- Models: `voron-rag-v2` (printer setting v2) and `voron-rag` (no printer setting).
- Citations are numbered links (`[1]`); each opens `/source/<doc>` on the server in a new tab with the cited chunk highlighted, plus a link to the original page. Set `PUBLIC_BASE_URL` when the server is reached under another address (default `http://localhost:8765`); spec: `docs/spec/citation-links-spec.md`.
- Each message is answered standalone, like `ask`; chat history is ignored. Cost per message is the same as `ask`.
- While `serve` runs, other commands cannot open the local Qdrant index (one process at a time).
- If `serve` is not running, LibreChat spins for about a minute and then shows "Connection error".

## Tests

```bash
uv run pytest      # 58 offline tests, no API key or network needed
```

## Data and licenses

- `data/raw/voron24_cases.jsonl`: 11 independently summarized community cases. They are first-person reports, not verified service records; resolution status is kept as recorded and never upgraded.
- Official docs (Voron-Documentation, Klipper) are GPL-3.0 and are not redistributed here; `fetch-docs` downloads them at pinned commits.
- Forum posts are not copied; cases are summaries with source URLs.
- Dependencies are MIT / Apache-2.0 / BSD (checked in M0, M6, M9).

## Status and known limits

See `ROADMAP.md` for decisions and `docs/milestones/M10_handoff.md` for open weaknesses and next experiments.
