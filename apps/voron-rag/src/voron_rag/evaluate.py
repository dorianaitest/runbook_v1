"""Evaluation against hand-reviewed labels in data/eval/questions.jsonl.

Retrieval: Hit@1, Recall@k and MRR per question, at section level (exact chunk) and source level
(any chunk of the right document; the only fair level for the recursive chunker, which has no sections).
Answers: one `answer_question` run per question, scored on behavior, citations and cost;
groundedness is graded by a human from the saved results, not by an LLM.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from langchain_core.documents import Document

QUESTIONS_PATH = Path(__file__).resolve().parents[2] / "data/eval/questions.jsonl"
RESULTS_DIR = QUESTIONS_PATH.parent / "results"
LEVELS = ("section", "source")


def load_questions(path: Path = QUESTIONS_PATH) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def doc_id(doc: Document) -> str:
    # Qdrant results carry the readable ID in metadata (the point ID is a UUID); keyword results in .id.
    return doc.metadata.get("doc_id") or doc.id


def source_of(any_id: str) -> str:
    """D10#bed_mesh/2 -> D10, D10~r4 -> D10, C08 -> C08."""
    return any_id.split("#")[0].split("~")[0]


def matches(retrieved: str, expected: str, level: str) -> bool:
    if level == "source":
        return source_of(retrieved) == source_of(expected)
    # A section label also covers its continuation parts (/2) and repeated-heading variants (~2).
    return retrieved == expected or retrieved.startswith((expected + "/", expected + "~"))


def check_labels(questions: list[dict], corpus_ids: list[str]) -> None:
    """Fail fast on typos: every label must match at least one chunk at section level."""
    missing = [
        f"{q['qid']}: {label}" for q in questions for label in q["relevant"]
        if not any(matches(i, label, "section") for i in corpus_ids)
    ]
    if missing:
        raise ValueError(f"labels not found in corpus: {', '.join(missing)}")


@dataclass
class RetrievalScore:
    qid: str
    retrieved: list[str]
    hit_at_1: bool
    recall: float
    reciprocal_rank: float


def score_retrieval(q: dict, retrieved: list[str], level: str) -> RetrievalScore:
    expected = q["relevant"]
    if level == "source":
        expected = list(dict.fromkeys(source_of(e) for e in expected))  # D07#a + D07#b count as one source
    found = [e for e in expected if any(matches(r, e, level) for r in retrieved)]
    first = next((rank for rank, r in enumerate(retrieved, 1) if any(matches(r, e, level) for e in expected)), None)
    return RetrievalScore(
        qid=q["qid"],
        retrieved=retrieved,
        hit_at_1=first == 1,
        recall=len(found) / len(expected),
        reciprocal_rank=1 / first if first else 0.0,
    )


def evaluate_retrieval(
    questions: list[dict], retrieve: Callable[[str, int], list[Document]], k: int, level: str
) -> list[RetrievalScore]:
    # Abstain questions have no relevant documents; they are scored in the answer evaluation only.
    return [
        score_retrieval(q, [doc_id(d) for d in retrieve(q["question"], k)], level)
        for q in questions if q["relevant"]
    ]


_SENTENCE_END = re.compile(r"(?<=[.\]])\s+(?!\[)|\n")  # a trailing [ID] stays with its sentence
_POWER_OFF = re.compile(r"power (off|down)|remove power|powering off", re.I)
_CITATION = re.compile(r"\[(C\d{2}|D\d{2}[#~][^\]]*)\]")


def cited_power_off(text: str) -> list[str]:
    """Sentences with power-off advice AND an evidence citation; most such citations were wrong in M8.

    shortcut: keyword heuristic, a human still decides whether the cited source says it (D05/D09 sometimes do).
    """
    return [s.strip() for s in _SENTENCE_END.split(text) if _POWER_OFF.search(s) and _CITATION.search(s)]


def behavior_ok(q: dict, abstained: bool, text: str) -> bool:
    """`answer` / `abstain` must match the marker; `either` accepts both (e.g. a nonexistent fault code,
    where "Klipper has no numbered codes" is right as an answer or as an abstention).
    Optional `must_say` (regex) is a content check on top."""
    marker_ok = q["behavior"] == "either" or abstained == (q["behavior"] == "abstain")
    return marker_ok and (not q.get("must_say") or bool(re.search(q["must_say"], text, re.IGNORECASE)))


# LLM judge for content (Decisions API): replaces ever-growing `must_say` regexes for correct refusals.
# One shared input per reply, three yes/no questions (each one criterion; a combined instruction got ignored in calibration).
JUDGE_THRESHOLD = 0.5  # calibrated on data/eval/judge_calibration.jsonl; probabilities cluster near 0 and 1
JUDGE_QUESTIONS = [
    {"type": "predicate", "name": "follows_note", "instructions":
        "Does the reply satisfy the grading note? Judge the content, not wording, language or formatting."},
    {"type": "predicate", "name": "grounded_refusal", "instructions": (
        "Does the reply clearly say that its evidence or knowledge base does not cover the product, version, option "
        "or value asked about, without accepting a made-up detail as fact?")},
    {"type": "predicate", "name": "invents", "instructions": (
        "Does the reply present specific values, pins, part counts, lengths, config options or commands as correct "
        "for the user's case although the grading note says they must not be invented, guessed or transferred?")},
]


def judge_input(row: dict) -> str:
    return (f"Question: {row['question']}\n\nGrading note (what a correct reply must and must not do): {row['note']}"
            f"\n\nReply to grade:\n{row['answer']}")


def judge_verdict(probs: dict, expected_behavior: str, threshold: float = JUDGE_THRESHOLD) -> bool | None:
    """Answer expected: pass = follows the note.
    Otherwise: pass = (follows the note OR refuses on grounds of missing evidence) AND invents nothing.

    The note may state outside facts ("there is no Voron 2.5") the corpus cannot know; a grounded refusal is right there.
    `invents` is ignored for answerable questions: the judge does not see the evidence, so it flagged documented
    values (n02, n39) as invented.
    """
    if any(probs.get(q["name"]) is None for q in JUDGE_QUESTIONS):
        return None  # the model refused a question
    if expected_behavior == "answer":
        return probs["follows_note"] >= threshold
    return (probs["follows_note"] >= threshold or probs["grounded_refusal"] >= threshold) and probs["invents"] < threshold


def add_judgement(row: dict, judge, threshold: float = JUDGE_THRESHOLD) -> dict:
    """Adds judge probabilities and judge_ok to a scored answer row (in place) and returns it."""
    row["judge"] = judge(judge_input(row), JUDGE_QUESTIONS)
    row["judge_ok"] = judge_verdict(row["judge"], row["expected_behavior"], threshold)
    return row


def score_answer(q: dict, result) -> dict:
    """Automatic checks on one AnswerResult; `grounded` and `rubric_notes` are left for the human grader."""
    return {
        "qid": q["qid"],
        "category": q["category"],
        "question": q["question"],
        "note": q["note"],
        "expected_behavior": q["behavior"],
        "abstained": result.abstained,
        "behavior_ok": behavior_ok(q, result.abstained, result.text),
        # Source level: citing D10#bed_mesh/2 or another C-case from the label set both count as "found it".
        "expected_source_cited": None if not q["relevant"] else any(
            matches(c, e, "source") for c in result.cited for e in q["relevant"]
        ),
        "cited": result.cited,
        "invalid_citations": result.invalid_citations,
        "cited_power_off": cited_power_off(result.text),
        "evidence": [doc_id(d) for d in result.evidence],
        "input_tokens": result.usage.get("input_tokens", 0),
        "output_tokens": result.usage.get("output_tokens", 0),
        "seconds": round(result.seconds, 2),
        "answer": result.text,
        "grounded": None,  # human: "yes" | "partly" | "no"
        "rubric_notes": "",
    }


def summarize(scores: list[RetrievalScore]) -> dict[str, float]:
    n = len(scores)
    return {
        "hit@1": sum(s.hit_at_1 for s in scores) / n,
        "recall@k": sum(s.recall for s in scores) / n,
        "mrr": sum(s.reciprocal_rank for s in scores) / n,
    }


def summarize_answers(rows: list[dict]) -> dict:
    """Counts for one answer run; input/output tokens are kept so the caller can price them."""
    answerable = [r for r in rows if r["expected_source_cited"] is not None]
    seconds = sorted(r["seconds"] for r in rows)
    return {
        # Plain counts (not "45/47" strings) so min/max over runs compares numbers.
        f"behavior_ok_of_{len(rows)}": sum(r["behavior_ok"] for r in rows),
        f"source_cited_of_{len(answerable)}": sum(bool(r["expected_source_cited"]) for r in answerable),
        **({f"judge_ok_of_{len(rows)}": sum(bool(r["judge_ok"]) for r in rows)} if "judge_ok" in rows[0] else {}),
        "invalid": sum(bool(r["invalid_citations"]) for r in rows),
        "cited_power_off": sum(len(r["cited_power_off"]) for r in rows),
        "median_s": seconds[len(seconds) // 2],
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
    }
