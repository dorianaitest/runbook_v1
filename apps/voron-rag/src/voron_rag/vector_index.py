"""Persistent semantic index in Qdrant local mode, with incremental (hash-based) updates.

Two separate jobs, deliberately separate functions:
- sync_index(): ingestion. Embeds only new or changed documents. Costs money when something changed.
- open_store() + similarity search: query time. Embeds only the query, never re-indexes.
"""

import atexit
import functools
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import tiktoken
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_qdrant import QdrantVectorStore, RetrievalMode, SparseEmbeddings
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, FieldCondition, Filter, MatchAny, Modifier, SparseVectorParams, VectorParams,
)

INDEX_DIR = Path(__file__).resolve().parents[2] / "data/index/qdrant"

DENSE, SPARSE = "dense", "bm25"  # vector names inside hybrid collections

# Fixed namespace so the same case_id always maps to the same UUID, on any machine.
_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "voron-rag")


def collection_name(embedding_model: str, chunker: str = "heading", hybrid: bool = False) -> str:
    # The model is part of the name: vectors from different models are not comparable,
    # so a model change creates a new, empty collection instead of mixing vector spaces.
    # The chunker is too, so two chunking strategies can be compared side by side.
    # Hybrid collections hold an extra sparse (BM25) vector per point, so they are separate collections.
    return f"voron_kb__{chunker}__{embedding_model}" + ("__hybrid" if hybrid else "")


def point_id(doc_id: str) -> str:
    # Qdrant only accepts UUIDs or integers as point IDs; uuid5 is deterministic.
    return str(uuid.uuid5(_ID_NAMESPACE, doc_id))


def content_hash(doc: Document) -> str:
    # Hash text AND metadata: a corrected status or URL must also reach the index.
    payload = {"page_content": doc.page_content, "metadata": doc.metadata}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


@functools.cache
def open_client(path: Path = INDEX_DIR) -> QdrantClient:
    # Local mode: Qdrant runs inside this Python process and stores data in `path`.
    # Only one client can open the same path at a time, so one cached client per path per process.
    path.mkdir(parents=True, exist_ok=True)
    client = QdrantClient(path=str(path))
    atexit.register(client.close)  # a cached client otherwise closes during interpreter shutdown and prints an ImportError
    return client


@dataclass
class IndexReport:
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    embedded_tokens: int = 0


def sync_index(
    docs: list[Document], embeddings: Embeddings, client: QdrantClient, collection: str,
    sparse: SparseEmbeddings | None = None,
) -> IndexReport:
    """Make the collection match `docs`, embedding only what is new or changed."""
    report = IndexReport()

    if not client.collection_exists(collection):
        # The collection needs the vector size up front; one tiny probe call tells us.
        dim = len(embeddings.embed_query("dimension probe"))
        dense = VectorParams(size=dim, distance=Distance.COSINE)
        if sparse is None:
            client.create_collection(collection, vectors_config=dense)
        else:
            # BM25 = term frequency (computed locally by fastembed) x IDF (computed by Qdrant over the
            # whole collection, hence Modifier.IDF; it stays correct as documents are added).
            client.create_collection(
                collection,
                vectors_config={DENSE: dense},
                sparse_vectors_config={SPARSE: SparseVectorParams(modifier=Modifier.IDF)},
            )

    store = _store(client, collection, embeddings, sparse)

    # What is already stored? Map point ID -> stored content hash.
    ids = [point_id(doc.id) for doc in docs]
    stored = {p.id: p.payload["metadata"].get("content_hash") for p in client.retrieve(collection, ids, with_payload=True)}

    to_embed: list[Document] = []
    for doc, pid in zip(docs, ids):
        doc_hash = content_hash(doc)
        if pid not in stored:
            report.new.append(doc.id)
        elif stored[pid] != doc_hash:
            report.changed.append(doc.id)
        else:
            report.unchanged.append(doc.id)
            continue
        # doc_id is stored because Qdrant only keeps the UUID; we need the readable ID back.
        meta = doc.metadata | {"content_hash": doc_hash, "doc_id": doc.id}
        to_embed.append(Document(id=doc.id, page_content=doc.page_content, metadata=meta))

    if to_embed:
        # Same IDs overwrite (upsert) the old points instead of adding duplicates.
        store.add_documents(to_embed, ids=[point_id(d.id) for d in to_embed])
        encoder = tiktoken.get_encoding("cl100k_base")
        report.embedded_tokens = sum(len(encoder.encode(d.page_content)) for d in to_embed)

    # Points whose source document disappeared would otherwise keep showing up in results.
    wanted = set(ids)
    all_points, _ = client.scroll(collection, limit=100_000, with_payload=True)  # shortcut: one page, fine below 100k points
    stale = [p for p in all_points if p.id not in wanted]
    if stale:
        client.delete(collection, points_selector=[p.id for p in stale])
        report.deleted = sorted(p.payload["metadata"].get("doc_id", "?") for p in stale)

    return report


RRF_K = 60  # standard RRF constant: damps the gap between rank 1 and rank 2
FUSION_DEPTH = 20  # each list is fetched this deep, so a chunk at rank 8 in both lists can still make the top 6


def rrf_fuse(rankings: list[list[Document]]) -> list[Document]:
    """Reciprocal Rank Fusion across separate searches: score = sum of 1 / (RRF_K + rank)."""
    scores: dict[str, float] = {}
    docs: dict[str, Document] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            key = doc.metadata.get("doc_id") or doc.id
            scores[key] = scores.get(key, 0.0) + 1 / (RRF_K + rank)
            docs.setdefault(key, doc)
    # sorted() is stable: on a tie the chunk seen first (the original question's ranking) wins.
    return [docs[key] for key in sorted(scores, key=scores.get, reverse=True)]


def printer_filter(printers: list[str]) -> Filter | None:
    """Hard filter: keep chunks for these printers or for no printer in particular; None (no filter) if unknown."""
    if not printers:
        return None
    return Filter(must=[FieldCondition(key="metadata.printer_models", match=MatchAny(any=[*printers, "any"]))])


def rerank_docs(question: str, docs: list[Document], encoder) -> list[Document]:
    """Order docs by cross-encoder relevance: the model reads question and chunk together, unlike embeddings."""
    scores = list(encoder.rerank(question, [d.page_content for d in docs]))
    return [d for _, d in sorted(zip(scores, docs), key=lambda pair: pair[0], reverse=True)]


def open_store(
    embeddings: Embeddings, client: QdrantClient, collection: str, sparse: SparseEmbeddings | None = None
) -> QdrantVectorStore:
    """Query-time access. Refuses to create or fill anything."""
    if not client.collection_exists(collection):
        raise RuntimeError(f"Collection {collection!r} does not exist. Run `uv run voron-rag index` first.")
    return _store(client, collection, embeddings, sparse)


def _store(
    client: QdrantClient, collection: str, embeddings: Embeddings, sparse: SparseEmbeddings | None = None
) -> QdrantVectorStore:
    # validate_collection_config=False: the default check embeds "dummy_text" on every
    # construction, a hidden billable API call per query. Wrong-size vectors still fail loudly
    # in Qdrant, and the model-specific collection name prevents mixing models.
    if sparse is None:
        return QdrantVectorStore(
            client=client, collection_name=collection, embedding=embeddings, validate_collection_config=False
        )
    # Hybrid: one dense and one BM25 search, merged by Reciprocal Rank Fusion (RRF) inside Qdrant.
    return QdrantVectorStore(
        client=client, collection_name=collection, embedding=embeddings, validate_collection_config=False,
        sparse_embedding=sparse, retrieval_mode=RetrievalMode.HYBRID, vector_name=DENSE, sparse_vector_name=SPARSE,
    )
