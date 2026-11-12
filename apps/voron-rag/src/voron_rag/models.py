"""The only place that knows which AI provider we use.

Everything else asks for "a chat model" or "an embedding model" through these functions.
A provider swap would change only this file, not the retrieval or answer code.
"""

import os

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

DEFAULT_CHAT_MODEL = "gpt-6-luna"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"

# gpt-6-luna is a reasoning model: it "thinks" in hidden tokens that are billed as output.
# Summarizing given evidence needs little thinking, so we keep it low for cost and latency.
REASONING_EFFORT = "low"

# Hard ceiling per answer. For reasoning models this budget includes the hidden reasoning
# tokens, so setting it too low can produce an empty answer.
MAX_OUTPUT_TOKENS = 800

# Without a timeout, a connection dropped mid-request (e.g. laptop sleep) hangs forever; with it, the client retries.
REQUEST_TIMEOUT_S = 60


def chat_model_name() -> str:
    return os.environ.get("OPENAI_CHAT_MODEL") or DEFAULT_CHAT_MODEL


def embedding_model_name() -> str:
    return os.environ.get("OPENAI_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL


def require_api_key() -> None:
    """Fail early with a helpful message. Never returns, prints or logs the key itself."""
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError("OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in.")


def get_chat_model() -> ChatOpenAI:
    require_api_key()
    # No api_key argument: the OpenAI client reads OPENAI_API_KEY from the environment itself,
    # so the secret never passes through our code.
    return ChatOpenAI(
        model=chat_model_name(),
        reasoning_effort=REASONING_EFFORT,
        max_tokens=MAX_OUTPUT_TOKENS,
        timeout=REQUEST_TIMEOUT_S,
    )


def get_embeddings() -> OpenAIEmbeddings:
    require_api_key()
    # Always pass the model explicitly: OpenAIEmbeddings' own default is the old ada-002.
    return OpenAIEmbeddings(model=embedding_model_name(), timeout=REQUEST_TIMEOUT_S)


# Local cross-encoders (fastembed, no API call), both licensed for commercial use.
# Rejected: jina-reranker-v2 (CC-BY-NC-4.0) and ms-marco-MiniLM (trained on MS MARCO, non-commercial terms).
RERANKERS = {
    # Apache-2.0, multilingual (German). fastembed pulls onnx-community's ONNX conversion, whose card shows no license.
    "bge-m3": "BAAI/bge-reranker-v2-m3-int8",
    "bge-base": "BAAI/bge-reranker-base",  # MIT, "commercial purposes free of charge"; English and Chinese only
    "llm": None,  # LLMReranker: OpenAI chat call, for machines too weak for a local model
}

# Probed 2026-11-08: with reasoning_effort="none" gpt-6-luna accepts temperature=0 and spends no hidden tokens.
# Rejected: gpt-5-nano (LangChain silently drops temperature, 512 reasoning tokens to rank 2 items), gpt-4o-mini (deprecated, pricier).
RERANK_MODEL = "gpt-6-luna"

RERANK_PROMPT = """You rank search results for a question about the Voron 2.4 3D printer and Klipper.
Return the ids of ALL candidates, most useful for answering the question first.
Prefer passages that directly answer it or contain the exact error, config option or part it names."""


class LLMReranker:
    """Listwise reranking: one chat call sees the question and all candidates and returns their order."""

    def __init__(self, log: list | None = None) -> None:
        from pydantic import BaseModel

        class Ranking(BaseModel):
            ids: list[int]

        require_api_key()
        llm = ChatOpenAI(model=RERANK_MODEL, reasoning_effort="none", temperature=0, timeout=REQUEST_TIMEOUT_S)
        self.llm = llm.with_structured_output(Ranking, method="json_schema", strict=True, include_raw=True)
        self.log = log  # one {"model", "usage"} entry per call, for cost reporting

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        from langchain_core.messages import HumanMessage, SystemMessage

        candidates = "\n\n".join(f"<candidate id={i}>\n{t}\n</candidate>" for i, t in enumerate(texts))
        out = self.llm.invoke([SystemMessage(RERANK_PROMPT), HumanMessage(f"Question: {query}\n\n{candidates}")])
        if self.log is not None:
            self.log.append({"model": RERANK_MODEL, "usage": out["raw"].usage_metadata or {}})
        if out["parsed"] is None:
            raise RuntimeError(f"Reranker returned no valid ranking: {out['parsing_error']}")
        return ranking_to_scores(out["parsed"].ids, len(texts))


def ranking_to_scores(ids: list[int], n: int) -> list[float]:
    """Rank list -> one score per candidate. Unknown/duplicate ids are ignored; omitted candidates go last in original order."""
    scores = [float(-n - i) for i in range(n)]
    valid = [i for i in dict.fromkeys(ids) if 0 <= i < n]  # dict.fromkeys dedups, keeping first occurrence
    for pos, i in enumerate(valid):
        scores[i] = float(-pos)
    return scores


def get_reranker(name: str, log: list | None = None):
    if name == "llm":
        return LLMReranker(log)

    from fastembed.rerank.cross_encoder import TextCrossEncoder

    from voron_rag.vector_index import INDEX_DIR

    return TextCrossEncoder(model_name=RERANKERS[name], cache_dir=str(INDEX_DIR.parent / "fastembed"))


def get_sparse_embeddings():
    """Local BM25 term weights for hybrid search (fastembed, no API call, no cost).

    The model files (Qdrant/bm25, Apache-2.0) are downloaded once to a local cache on first use.
    """
    from langchain_qdrant import FastEmbedSparse

    from voron_rag.vector_index import INDEX_DIR

    # Default cache is /tmp (wiped on reboot); keep it next to the git-ignored index instead.
    return FastEmbedSparse(model_name="Qdrant/bm25", cache_dir=str(INDEX_DIR.parent / "fastembed"))


# Decisions API (POST /v1/decisions): typed yes/no answers with a probability, no free text.
# gpt-6-luna is the only supported model; $0.10 per 1M input tokens, output free (pricing page, 2026-11-09).
JUDGE_MODEL = "gpt-6-luna"
JUDGE_USD_PER_M_INPUT = 0.10


def get_judge(log: list | None = None):
    """judge(input, questions) -> {question name: probability}; None where the model refused that question."""
    from openai import OpenAI

    require_api_key()
    client = OpenAI(timeout=REQUEST_TIMEOUT_S)

    def judge(text: str, questions: list[dict]) -> dict[str, float | None]:
        decision = client.decisions.create(model=JUDGE_MODEL, input=text, questions=questions)
        if log is not None:
            log.append({"model": JUDGE_MODEL, "input_tokens": decision.usage.input_tokens})
        return {a.name: getattr(a, "probability", None) for a in decision.answers}

    return judge


def judge_cost(log: list) -> float:
    return sum(e["input_tokens"] for e in log) * JUDGE_USD_PER_M_INPUT / 1e6
