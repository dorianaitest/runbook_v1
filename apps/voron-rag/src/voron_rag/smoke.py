"""One deliberately tiny, billable round trip to OpenAI: one chat call, one embedding."""

import math

import tiktoken

from voron_rag import models

# USD per 1M tokens, from developers.openai.com/api/docs/pricing on 2026-11-01. Re-check before relying on it.
PRICE_PER_M = {
    "gpt-6-luna": {"input": 0.10, "output": 0.50},
    "text-embedding-3-small": {"input": 0.02},
}


def chat_cost(model: str, usage: dict) -> float | None:
    """Estimated USD for one chat call; None if the model has no known price."""
    price = PRICE_PER_M.get(model)
    if not price or not usage:
        return None
    return usage["input_tokens"] * price["input"] / 1e6 + usage["output_tokens"] * price["output"] / 1e6


def run_smoke_test() -> None:
    # --- 1. Chat model: generates text. Billed for input and output (including hidden reasoning).
    chat = models.get_chat_model()
    reply = chat.invoke("Reply with exactly one word: OK")
    usage = reply.usage_metadata or {}
    reasoning = (usage.get("output_token_details") or {}).get("reasoning", 0)
    print(f"[chat] model={models.chat_model_name()}")
    print(f"[chat] reply={reply.content!r}")
    print(f"[chat] input_tokens={usage.get('input_tokens')} output_tokens={usage.get('output_tokens')} (of which reasoning={reasoning})")

    price = PRICE_PER_M.get(models.chat_model_name())
    if price and usage:
        cost = usage["input_tokens"] * price["input"] / 1e6 + usage["output_tokens"] * price["output"] / 1e6
        print(f"[chat] estimated cost=${cost:.6f}")

    # --- 2. Embedding model: generates no text, only a vector. Billed for input tokens only.
    text = "MCU 'mcu' shutdown: ADC out of range"
    vector = models.get_embeddings().embed_query(text)
    # OpenAIEmbeddings does not return usage, so count tokens locally with the same tokenizer family.
    n_tokens = len(tiktoken.get_encoding("cl100k_base").encode(text))
    print(f"\n[embed] model={models.embedding_model_name()} text={text!r}")
    print(f"[embed] dimensions={len(vector)} first5={[round(v, 4) for v in vector[:5]]}")
    # OpenAI embeddings are normalized to length 1, so cosine similarity equals the dot product.
    print(f"[embed] vector length (L2 norm)={math.sqrt(sum(v * v for v in vector)):.4f}")
    emb_price = PRICE_PER_M.get(models.embedding_model_name())
    if emb_price:
        print(f"[embed] tokens~{n_tokens} estimated cost=${n_tokens * emb_price['input'] / 1e6:.8f}")
