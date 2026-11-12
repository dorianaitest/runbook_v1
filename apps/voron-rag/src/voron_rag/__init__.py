import argparse
from pathlib import Path

CHUNKERS = ("heading", "recursive")
BILLABLE = ("smoke", "index", "semantic", "compare", "ask", "serve")
PRINTERS = ("v0", "legacy", "v1", "trident", "v2", "switchwire")  # = docs.PRINTERS, kept here so --help stays import-light


def load_corpus(chunker: str):
    """All searchable documents: community cases plus official doc chunks."""
    from voron_rag.cases import load_case_documents
    from voron_rag.docs import load_doc_chunks

    chunks, report = load_doc_chunks(chunker)
    return load_case_documents() + chunks, report


def describe(meta: dict) -> str:
    """One line that makes the authority level impossible to miss."""
    if meta.get("authority") == "community_case":
        return f"[COMMUNITY CASE, {meta['resolution_status']}] {meta['error_signature']}"
    return f"[OFFICIAL DOC, {meta.get('publisher')}, board={meta.get('board_variant')}] {meta.get('heading_path')}"


def semantic_retriever(
    chunker: str, hybrid: bool = False, hyde_log: list | None = None, hyde: str = "replace", rerank: str | None = None,
    rerank_log: list | None = None, printer: str | None = None,
):
    """retrieve(question, k) over the Qdrant collection (one query embedding per call; BM25 part is local).

    With hyde_log (a list), HyDE is on: one extra chat call per question, logged into that list.
    hyde="replace" searches with the HyDE query only; "fuse" also searches with the original
    question and merges both rankings with RRF, so the user's own wording keeps a vote.
    rerank (a key of models.RERANKERS) fetches a deeper candidate list and lets a local
    cross-encoder (or, with "llm", a chat model) pick the top k by reading each chunk together with the question.
    The llm reranker logs one usage entry per call into rerank_log.
    Searches are filtered to the printers in docs.target_printers(question, printer), plus printer-independent chunks.
    """
    base = _base_retriever(chunker, hybrid, hyde_log, hyde, printer)
    if rerank is None:
        return base

    from voron_rag.models import get_reranker
    from voron_rag.vector_index import FUSION_DEPTH, rerank_docs

    encoder = get_reranker(rerank, rerank_log)
    return lambda question, k: rerank_docs(question, base(question, max(k, FUSION_DEPTH)), encoder)[:k]


def _base_retriever(chunker: str, hybrid: bool, hyde_log: list | None, hyde: str, printer: str | None = None):
    from voron_rag.docs import target_printers
    from voron_rag.models import embedding_model_name, get_embeddings, get_sparse_embeddings
    from voron_rag.vector_index import collection_name, open_client, open_store, printer_filter

    sparse = get_sparse_embeddings() if hybrid else None
    store = open_store(get_embeddings(), open_client(), collection_name(embedding_model_name(), chunker, hybrid), sparse)

    def search(query: str, question: str, k: int):
        # The filter comes from the user's own question, never from a HyDE passage.
        return store.similarity_search(query, k=k, filter=printer_filter(target_printers(question, printer)))

    if hyde_log is None:
        return lambda question, k: search(question, question, k)

    from voron_rag.answer import hyde_query
    from voron_rag.models import get_chat_model

    chat = get_chat_model()
    if hyde == "replace":
        return lambda question, k: search(hyde_query(question, chat, hyde_log), question, k)

    from voron_rag.vector_index import FUSION_DEPTH, rrf_fuse

    def fused(question: str, k: int):
        depth = max(k, FUSION_DEPTH)
        rankings = [search(q, question, depth) for q in (question, hyde_query(question, chat, hyde_log))]
        return rrf_fuse(rankings)[:k]

    return fused


def calls_cost(log: list) -> float:
    """USD for logged extra chat calls (HyDE, LLM rerank); entries without a model used the chat model."""
    from voron_rag.models import chat_model_name
    from voron_rag.smoke import chat_cost

    return sum(chat_cost(e.get("model") or chat_model_name(), e["usage"]) or 0.0 for e in log)


def main(argv: list[str] | None = None) -> None:
    import openai

    try:
        _run(argv)
    except openai.APIConnectionError:
        # Semantic search and answers need the API; a full traceback would hide the actual problem.
        raise SystemExit(
            "Cannot reach the OpenAI API (network or DNS problem). "
            "Offline keyword search still works: uv run voron-rag search \"...\""
        )


def _run(argv: list[str] | None) -> None:
    # argv=None means "read the real command line"; tests pass a list instead.
    parser = argparse.ArgumentParser(prog="voron-rag", description="Voron 2.4 knowledge assistant")
    commands = parser.add_subparsers(dest="command", required=True)

    cases_cmd = commands.add_parser("cases", help="list the community cases (offline)")
    cases_cmd.add_argument("--show", type=int, default=2, help="how many documents to print in full")

    commands.add_parser("fetch-docs", help="download the approved docs at pinned commits (network, free)")

    search_cmd = commands.add_parser("search", help="keyword search over cases and docs (offline)")
    search_cmd.add_argument("query", help='question; put exact phrases in double quotes, e.g. \'"ADC out of range"\'')
    search_cmd.add_argument("-k", type=int, default=3, help="number of results")
    search_cmd.add_argument("--chunker", choices=CHUNKERS, default="heading")

    commands.add_parser("config", help="show model configuration and whether a key is set (offline)")
    commands.add_parser("smoke", help="one tiny chat call and one embedding (BILLABLE, about $0.0001)")

    index_cmd = commands.add_parser("index", help="embed new/changed documents into Qdrant (BILLABLE only for changes)")
    index_cmd.add_argument("--chunker", choices=CHUNKERS, default="heading")
    index_cmd.add_argument("--dry-run", action="store_true", help="only show the ingestion report, no API calls")
    index_cmd.add_argument("--hybrid", action=argparse.BooleanOptionalAction, default=True, help="dense + BM25 collection (default); --no-hybrid for dense only")

    semantic_cmd = commands.add_parser("semantic", help="semantic search (BILLABLE: one query embedding)")
    semantic_cmd.add_argument("query")
    semantic_cmd.add_argument("-k", type=int, default=3)
    semantic_cmd.add_argument("--chunker", choices=CHUNKERS, default="heading")

    compare_cmd = commands.add_parser("compare", help="keyword vs semantic top-k per query (BILLABLE: one embedding per query)")
    compare_cmd.add_argument("queries", nargs="+")
    compare_cmd.add_argument("-k", type=int, default=3)
    compare_cmd.add_argument("--chunker", choices=CHUNKERS, default="heading")

    ask_cmd = commands.add_parser("ask", help="grounded answer with citations (BILLABLE: 1 embedding + 2 chat calls incl. rerank)")
    ask_cmd.add_argument("question")
    ask_cmd.add_argument("-k", type=int, default=6, help="evidence chunks to retrieve")
    ask_cmd.add_argument("--chunker", choices=CHUNKERS, default="heading")
    ask_cmd.add_argument("--hybrid", action=argparse.BooleanOptionalAction, default=True, help="dense + BM25 fused with RRF (default)")
    ask_cmd.add_argument("--rerank", choices=("llm", "none", "bge-m3", "bge-base"), default=None, help="rerank a top-20 candidate list: llm = gpt-6-luna, default for semantic/hybrid (BILLABLE: +1 chat call per question); none = off; bge-* = local cross-encoder (free, needs a strong machine)")
    ask_cmd.add_argument("--printer", choices=PRINTERS, default=None, help="the user's printer: retrieval keeps only chunks for it, printers the question names, and printer-independent chunks")
    ask_cmd.add_argument("--hyde", choices=("replace", "fuse"), default=None, help="HyDE: search with a hypothetical doc passage; replace = passage query only, fuse = RRF with the original question (BILLABLE: +1 chat call per question)")
    ask_cmd.add_argument("--debug", action="store_true", help="print the exact evidence sent to the model first")

    serve_cmd = commands.add_parser("serve", help="OpenAI-compatible API for chat UIs like LibreChat (BILLABLE per message, like ask); needs VORON_RAG_API_KEY")
    # 0.0.0.0 so the LibreChat container can reach it (127.0.0.1 leaves LibreChat spinning, then "Connection error"); the bearer key guards it.
    serve_cmd.add_argument("--host", default="0.0.0.0", help="interface to listen on (default 0.0.0.0, reachable from Docker containers)")
    serve_cmd.add_argument("--port", type=int, default=8765)

    eval_r = commands.add_parser("eval-retrieval", help="Hit@1/Recall@k/MRR on the labeled questions (semantic is BILLABLE: ~20 query embeddings)")
    eval_r.add_argument("--retriever", choices=("keyword", "semantic", "hybrid"), default="keyword")
    eval_r.add_argument("--chunker", choices=CHUNKERS, default="heading")
    eval_r.add_argument("-k", type=int, default=6)
    eval_r.add_argument("--questions", default=None, help="question file (default data/eval/questions.jsonl)")
    eval_r.add_argument("--rerank", choices=("llm", "none", "bge-m3", "bge-base"), default=None, help="rerank a top-20 candidate list: llm = gpt-6-luna, default for semantic/hybrid (BILLABLE: +1 chat call per question); none = off; bge-* = local cross-encoder (free, needs a strong machine)")
    eval_r.add_argument("--printer", choices=PRINTERS, default=None, help="the user's printer: retrieval keeps only chunks for it, printers the question names, and printer-independent chunks")
    eval_r.add_argument("--hyde", choices=("replace", "fuse"), default=None, help="HyDE: search with a hypothetical doc passage; replace = passage query only, fuse = RRF with the original question (BILLABLE: +1 chat call per question)")

    eval_a = commands.add_parser("eval-answers", help="answer every labeled question and save results (BILLABLE: 1 embedding + 2 chat calls per question, incl. rerank)")
    eval_a.add_argument("--chunker", choices=CHUNKERS, default="heading")
    eval_a.add_argument("--hybrid", action=argparse.BooleanOptionalAction, default=True)
    eval_a.add_argument("--repeat", type=int, default=1, help="run the whole set N times to measure run-to-run noise")
    eval_a.add_argument("-k", type=int, default=6)
    eval_a.add_argument("--questions", default=None, help="question file (default data/eval/questions.jsonl)")
    eval_a.add_argument("--rerank", choices=("llm", "none", "bge-m3", "bge-base"), default=None, help="rerank a top-20 candidate list: llm = gpt-6-luna, default for semantic/hybrid (BILLABLE: +1 chat call per question); none = off; bge-* = local cross-encoder (free, needs a strong machine)")
    eval_a.add_argument("--printer", choices=PRINTERS, default=None, help="the user's printer: retrieval keeps only chunks for it, printers the question names, and printer-independent chunks")
    eval_a.add_argument("--hyde", choices=("replace", "fuse"), default=None, help="HyDE: search with a hypothetical doc passage; replace = passage query only, fuse = RRF with the original question (BILLABLE: +1 chat call per question)")

    eval_a.add_argument("--judge", action="store_true", help="also grade each reply with the Decisions API judge (BILLABLE: ~$0.0001 per question)")

    eval_j = commands.add_parser("eval-judge", help="grade saved answer rows with the Decisions API judge, in place (BILLABLE: ~$0.0001 per row)")
    eval_j.add_argument("files", nargs="+", help="results/*.jsonl or the calibration file; rows with human_ok also get an agreement table")

    args = parser.parse_args(argv)
    if hasattr(args, "rerank"):
        # Unset means the default: LLM rerank, except for the offline keyword retriever.
        if args.rerank is None:
            args.rerank = None if getattr(args, "retriever", None) == "keyword" else "llm"
        elif args.rerank == "none":
            args.rerank = None

    if args.command in BILLABLE + ("config", "eval-retrieval", "eval-answers", "eval-judge"):
        from dotenv import load_dotenv

        # Copies values from .env into this process's environment. override=False: a variable
        # already set in the shell wins over the file. Only commands that need the API load it.
        load_dotenv(override=False)

    if args.command == "cases":
        from voron_rag.cases import load_case_documents

        docs = load_case_documents()
        print(f"{len(docs)} cases loaded")
        for doc in docs[: args.show]:
            print(f"\n=== {doc.id} ===")
            print(doc.page_content)
            print("--- metadata ---")
            for key, value in doc.metadata.items():
                print(f"{key}: {value}")

    elif args.command == "fetch-docs":
        from voron_rag.sources import fetch_docs

        for source_id, status in fetch_docs():
            print(f"{source_id}: {status}")

    elif args.command == "search":
        from voron_rag.keyword_search import keyword_search

        docs, _ = load_corpus(args.chunker)
        hits = keyword_search(args.query, docs, k=args.k)
        if not hits:
            print("No matching documents.")
        for rank, hit in enumerate(hits, start=1):
            print(f"{rank}. {hit.doc.id}  score={hit.score:.2f}  {describe(hit.doc.metadata)}")
            phrases = f"; phrases: {', '.join(hit.matched_phrases)}" if hit.matched_phrases else ""
            print(f"   matched: {', '.join(hit.matched_terms)}{phrases}")
            print(f"   {hit.doc.metadata['source_url']}")

    elif args.command == "config":
        import os

        from voron_rag import models

        # Only yes/no for the key: no length, no prefix, nothing that helps reconstruct it.
        print(f"OPENAI_API_KEY set: {'yes' if os.environ.get('OPENAI_API_KEY', '').strip() else 'NO'}")
        print(f"chat model:      {models.chat_model_name()} (reasoning_effort={models.REASONING_EFFORT}, max_tokens={models.MAX_OUTPUT_TOKENS})")
        print(f"embedding model: {models.embedding_model_name()}")

    elif args.command == "smoke":
        from voron_rag.smoke import run_smoke_test

        run_smoke_test()

    elif args.command == "index":
        from voron_rag.models import embedding_model_name

        docs, docs_report = load_corpus(args.chunker)
        print(f"sources={docs_report.sources} sections kept={docs_report.sections_kept}/{docs_report.sections_total} "
              f"doc chunks={docs_report.chunks} (+{len(docs) - docs_report.chunks} cases)")
        print(f"doc tokens~{docs_report.tokens}  largest chunk~{docs_report.max_chunk_tokens} tokens")
        for failure in docs_report.failures:
            print(f"  FAILURE: {failure}")
        if args.dry_run:
            print("dry run: nothing embedded")
            return

        from voron_rag.models import get_embeddings, get_sparse_embeddings
        from voron_rag.vector_index import collection_name, open_client, sync_index

        coll = collection_name(embedding_model_name(), args.chunker, args.hybrid)
        client = open_client()
        report = sync_index(docs, get_embeddings(), client, coll, get_sparse_embeddings() if args.hybrid else None)
        print(f"collection: {coll}  (points: {client.count(coll).count})")
        print(f"new={len(report.new)} changed={len(report.changed)} unchanged={len(report.unchanged)} deleted={len(report.deleted)}")
        if report.deleted:
            print(f"  deleted: {', '.join(report.deleted)}")
        print(f"embedded tokens~{report.embedded_tokens}  estimated cost=${report.embedded_tokens * 0.02 / 1e6:.6f}")

    elif args.command in ("semantic", "compare"):
        from voron_rag.models import embedding_model_name, get_embeddings
        from voron_rag.vector_index import collection_name, open_client, open_store

        store = open_store(get_embeddings(), open_client(), collection_name(embedding_model_name(), args.chunker))

        if args.command == "semantic":
            # Qdrant returns cosine similarity: 1.0 = same direction, ~0 = unrelated. Higher is better.
            for rank, (doc, score) in enumerate(store.similarity_search_with_score(args.query, k=args.k), start=1):
                print(f"{rank}. {doc.metadata['doc_id']}  cosine={score:.3f}  {describe(doc.metadata)}")
                print(f"   {doc.metadata['source_url']}")
        else:
            from voron_rag.keyword_search import keyword_search

            docs, _ = load_corpus(args.chunker)
            for query in args.queries:
                kw = [f"{h.doc.id}({h.score:.1f})" for h in keyword_search(query, docs, k=args.k)]
                sem = [f"{d.metadata['doc_id']}({s:.2f})" for d, s in store.similarity_search_with_score(query, k=args.k)]
                print(f"\n{query}")
                print(f"  keyword : {' '.join(kw) or '-'}")
                print(f"  semantic: {' '.join(sem)}")

    elif args.command == "ask":
        from voron_rag.answer import answer_question, sources_block
        from voron_rag.models import chat_model_name, get_chat_model
        from voron_rag.smoke import chat_cost

        hyde_log = [] if args.hyde else None
        rerank_log: list = []
        result = answer_question(args.question, semantic_retriever(args.chunker, args.hybrid, hyde_log, args.hyde, args.rerank, rerank_log, args.printer), get_chat_model(), k=args.k, printer=args.printer)

        if args.debug:
            # Shown so a wrong answer can be traced to retrieval (bad evidence) or generation (misused evidence).
            if hyde_log:
                print(f"=== HYDE PASSAGE (retrieval only) ===\n{hyde_log[0]['passage']}\n")
            print("=== EVIDENCE SENT TO THE MODEL ===")
            print(result.evidence_text)
            print("=== END EVIDENCE ===\n")

        print(result.text)
        if sources := sources_block(result):
            print(sources)

        usage = result.usage
        usd = chat_cost(chat_model_name(), usage)
        if usd is not None:
            usd += calls_cost((hyde_log or []) + rerank_log)
        cost = f"  cost~${usd:.5f}" if usd is not None else ""
        reasoning = (usage.get("output_token_details") or {}).get("reasoning", 0)
        print(f"\n[evidence: {', '.join(d.metadata['doc_id'] for d in result.evidence)}]")
        print(f"[{len(result.evidence)} evidence chunks | in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
              f"(reasoning={reasoning}) | {result.seconds:.1f}s{cost}]")

    elif args.command == "serve":
        import os

        from voron_rag.answer import answer_question
        from voron_rag.models import get_chat_model
        from voron_rag.serve import MODELS, linked_reply, serve, source_pages

        api_key = os.environ.get("VORON_RAG_API_KEY", "").strip()
        if not api_key:
            raise SystemExit("Set VORON_RAG_API_KEY in .env first; the server refuses to run without a key.")
        # Same defaults as `ask`: hybrid, heading chunker, LLM rerank, k=6. One retriever per printer setting.
        chat = get_chat_model()
        retrievers = {p: semantic_retriever("heading", True, rerank="llm", printer=p) for p in MODELS.values()}

        # Where the browser opens citation links: the viewer route of this server (a public URL on the VPS).
        base_url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/") or f"http://localhost:{args.port}"

        def answer(question: str, printer: str | None) -> str:
            return linked_reply(answer_question(question, retrievers[printer], chat, printer=printer), base_url)

        corpus, _ = load_corpus("heading")
        serve(answer, api_key, args.host, args.port, source_pages(corpus))

    elif args.command == "eval-retrieval":
        from voron_rag import evaluate as ev
        from voron_rag.keyword_search import keyword_search

        questions = ev.load_questions(Path(args.questions)) if args.questions else ev.load_questions()
        # Labels use heading-chunker section IDs, so they are checked against that corpus.
        ev.check_labels(questions, [d.id for d in load_corpus("heading")[0]])
        hyde_log = [] if args.hyde else None
        rerank_log: list = []
        if args.retriever == "keyword":
            if args.hyde or args.rerank or args.printer:
                raise SystemExit("--hyde, --rerank and --printer need --retriever semantic or hybrid")
            docs, _ = load_corpus(args.chunker)
            retrieve = lambda q, k: [h.doc for h in keyword_search(q, docs, k=k)]  # noqa: E731
        else:
            retrieve = semantic_retriever(args.chunker, args.retriever == "hybrid", hyde_log, args.hyde, args.rerank, rerank_log, args.printer)

        import functools

        import statistics
        import time

        timings: list[float] = []

        def timed(question: str, k: int, inner=retrieve):
            start = time.perf_counter()
            docs = inner(question, k)
            timings.append(time.perf_counter() - start)
            return docs

        # Both levels score the same retrieval; matters once retrieval is not deterministic (HyDE) and halves API calls.
        retrieve = functools.cache(timed)
        print(f"retriever={args.retriever} chunker={args.chunker} k={args.k} hyde={args.hyde} rerank={args.rerank} printer={args.printer}")
        scores = {lvl: ev.evaluate_retrieval(questions, retrieve, args.k, lvl) for lvl in ev.LEVELS}
        for sec, src in zip(scores["section"], scores["source"]):
            print(f"{sec.qid}  section: recall={sec.recall:.2f} rr={sec.reciprocal_rank:.2f}  "
                  f"source: recall={src.recall:.2f} rr={src.reciprocal_rank:.2f}  top: {' '.join(sec.retrieved[:3])}")
        for lvl in ev.LEVELS:
            summary = ev.summarize(scores[lvl])
            print(f"{lvl:>7} level ({len(scores[lvl])} q): " + "  ".join(f"{m}={v:.2f}" for m, v in summary.items()))
        print(f"retrieval latency per question: mean={statistics.mean(timings):.2f}s median={statistics.median(timings):.2f}s "
              f"max={max(timings):.2f}s (n={len(timings)})")
        if rerank_log:
            print(f"rerank: {len(rerank_log)} chat calls  cost~${calls_cost(rerank_log):.4f}")
        if hyde_log:
            import json

            # Kept so retrieval misses can be traced to a bad hypothetical passage.
            ev.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            out = ev.RESULTS_DIR / f"hyde-{args.hyde}_retrieval_{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
            out.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in hyde_log), encoding="utf-8")
            print(f"hyde: {len(hyde_log)} chat calls  cost~${calls_cost(hyde_log):.4f}  passages: {out}")

    elif args.command == "eval-answers":
        import json
        import time

        from voron_rag import evaluate as ev
        from voron_rag.answer import answer_question
        from voron_rag.models import chat_model_name, get_chat_model
        from voron_rag.smoke import chat_cost

        hyde_log = [] if args.hyde else None
        rerank_log: list = []
        from voron_rag.models import get_judge, judge_cost

        judge_log: list = []
        judge = get_judge(judge_log) if args.judge else None
        retrieve, chat = semantic_retriever(args.chunker, args.hybrid, hyde_log, args.hyde, args.rerank, rerank_log, args.printer), get_chat_model()
        questions = ev.load_questions(Path(args.questions)) if args.questions else ev.load_questions()
        ev.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        summaries = []
        for run in range(1, args.repeat + 1):
            rows = []
            for q in questions:
                row = ev.score_answer(q, answer_question(q["question"], retrieve, chat, k=args.k, printer=args.printer))
                if judge:
                    ev.add_judgement(row, judge)
                rows.append(row)
                if not row["behavior_ok"] or row["expected_source_cited"] is False or row["invalid_citations"]:
                    print(f"  run {run} {row['qid']}: behavior_ok={row['behavior_ok']} abstained={row['abstained']} "
                          f"source_cited={row['expected_source_cited']} invalid={row['invalid_citations'] or '-'}")
            suffix = f"_r{run}" if args.repeat > 1 else ""
            out = ev.RESULTS_DIR / f"answers_{args.chunker}{'_hybrid' if args.hybrid else ''}{f'_hyde-{args.hyde}' if args.hyde else ''}{f'_rerank-{args.rerank}' if args.rerank else ''}_{stamp}{suffix}.jsonl"
            out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            summary = ev.summarize_answers(rows)
            summary["cost_usd"] = chat_cost(chat_model_name(), summary) or 0.0
            summary["cost_usd"] += calls_cost((hyde_log or []) + rerank_log) + judge_cost(judge_log)
            rerank_log.clear()  # per-run cost
            judge_log.clear()
            if hyde_log:
                hyde_log.clear()
            summaries.append(summary)
            print(f"run {run}: " + "  ".join(f"{k}={v}" for k, v in summary.items() if k not in ("input_tokens", "output_tokens"))
                  + f"\n  saved: {out}")

        if args.repeat > 1:
            # Run-to-run spread: a later change only counts if it moves a metric outside this range.
            print("\nrange over runs (min-max):")
            for key in summaries[0]:
                values = [s[key] for s in summaries]
                print(f"  {key}: {min(values)}-{max(values)}" if key != "cost_usd" else f"  total cost~${sum(values):.4f}")
        print("fill in 'grounded' and 'rubric_notes' by hand")

    elif args.command == "eval-judge":
        import json

        from voron_rag import evaluate as ev
        from voron_rag.models import get_judge, judge_cost

        log: list = []
        judge = get_judge(log)
        for name in args.files:
            path = Path(name)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            for row in rows:
                ev.add_judgement(row, judge)
                j = row["judge"]
                print(f"{row['qid']}  regex_ok={row.get('behavior_ok')}  judge_ok={row['judge_ok']}  "
                      f"follows={j.get('follows_note')}  refusal={j.get('grounded_refusal')}  invents={j.get('invents')}  "
                      f"human_ok={row.get('human_ok', '-')}")
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            labeled = [r for r in rows if "human_ok" in r]
            if labeled:
                print(f"agreement with human_ok ({len(labeled)} rows, {sum(not r['human_ok'] for r in labeled)} bad):")
                for th in (0.3, 0.4, 0.5, 0.6, 0.7):
                    ok = [ev.judge_verdict(r["judge"], r["expected_behavior"], th) for r in labeled]
                    wrong = [r["qid"] + ("+" if r["human_ok"] else "-") for r, o in zip(labeled, ok) if o != r["human_ok"]]
                    print(f"  threshold {th}: {len(labeled) - len(wrong)}/{len(labeled)}  disagree: {' '.join(wrong) or '-'}")
                if "behavior_ok" in labeled[0]:
                    print(f"  regex must_say: {sum(r['behavior_ok'] == r['human_ok'] for r in labeled)}/{len(labeled)}")
            print(f"saved: {path}")
        print(f"judge: {len(log)} calls  cost~${judge_cost(log):.5f}")
