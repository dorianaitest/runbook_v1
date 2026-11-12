"""Transparent keyword search: the zero-cost baseline that semantic search has to beat.

Scoring, in plain words:
- Each query word found in a document adds its IDF weight: rare words count a lot,
  words that appear in every document count nothing.
- A "quoted phrase" found word-for-word in a document counts its words a second time.
That is all. No stemming, no synonyms, no length normalization (BM25 adds those ideas later).
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

from langchain_core.documents import Document

# Runs of Unicode letters/digits. Underscores, hyphens and punctuation split words, so
# "verify_heater", "verify heater" and "Verify-Heater" all become ["verify", "heater"].
# [^\W_] means "a word character that is not an underscore"; it also keeps German umlauts.
_TOKEN = re.compile(r"[^\W_]+")
_QUOTED = re.compile(r'"([^"]+)"')


def tokenize(text: str) -> list[str]:
    """Lowercase and split into words. Used for both documents and queries, which must match."""
    return _TOKEN.findall(text.lower())


@dataclass
class Hit:
    doc: Document
    score: float
    matched_terms: list[str]  # query words that were found and carry weight
    matched_phrases: list[str]  # quoted phrases found word-for-word


def _contains_phrase(tokens: list[str], phrase: list[str]) -> bool:
    n = len(phrase)
    return any(tokens[i : i + n] == phrase for i in range(len(tokens) - n + 1))


def keyword_search(query: str, docs: list[Document], k: int = 3) -> list[Hit]:
    """Return the top-k documents with a score above zero, best first."""
    # shortcut: tokenizes the whole corpus on every query; fine for hundreds of docs, precompute for more.
    doc_tokens = [tokenize(doc.page_content) for doc in docs]
    n_docs = len(docs)

    # Document frequency: in how many documents does each word appear (at least once)?
    df = Counter(word for tokens in doc_tokens for word in set(tokens))

    def idf(word: str) -> float:
        # log(N / df): a word in 1 of 11 docs -> 2.4, in all 11 -> 0.0.
        return math.log(n_docs / df[word])

    # Unique query words: typing a word twice should not double its weight.
    query_terms = set(tokenize(query))
    phrases = [p for p in (tokenize(q) for q in _QUOTED.findall(query)) if p]

    hits: list[Hit] = []
    for doc, tokens in zip(docs, doc_tokens):
        matched = sorted(t for t in query_terms & set(tokens) if idf(t) > 0)
        score = sum(idf(t) for t in matched)

        matched_phrases = [p for p in phrases if _contains_phrase(tokens, p)]
        score += sum(idf(t) for p in matched_phrases for t in p)

        if score > 0:
            hits.append(Hit(doc, score, matched, [" ".join(p) for p in matched_phrases]))

    # Highest score first; ties broken by ID so results are reproducible.
    hits.sort(key=lambda h: (-h.score, h.doc.id))
    return hits[:k]
