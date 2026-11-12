"""Deterministic 2-step RAG: retrieve evidence once, generate once, then check citations.

question -> retrieve(k) -> budget + label evidence -> ONE chat call -> validate citations
There are no loops, tools or retries: every question costs one query embedding and one chat call.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from langchain_core.documents import Document
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from voron_rag.docs import count_tokens

TOP_K = 6
EVIDENCE_TOKEN_BUDGET = 3000
ABSTAIN_MARKER = "INSUFFICIENT EVIDENCE:"
NOTE_LABEL = "KNOWLEDGE-BASE GUIDANCE (written by this project's maintainer, not from the original post)"

SYSTEM_PROMPT = f"""You are a read-only troubleshooting assistant for Voron 2.4 / 2.4r2 3D printers running Klipper.
Answer ONLY from the numbered evidence in the user message. You never control equipment.

Rules:
1. Every factual or actionable statement must end with a citation of the evidence ID in square brackets, e.g. [C08] or [D01#adc-error]. Cite only IDs that appear in the evidence. Do not write URLs.
2. Keep authority levels apart and say which one each statement comes from:
   - OFFICIAL DOCUMENTATION (Voron Design or Klipper)
   - COMMUNITY CASE - OWNER-CONFIRMED: worked on that owner's specific hardware; do not present it as universal.
   - COMMUNITY CASE - UNCONFIRMED OR PARTIAL / UNRESOLVED: hypotheses only, never a proven fix.
   - KNOWLEDGE-BASE GUIDANCE ([Cxx-note]): written by this project's maintainer, not by the original poster. Cite advice taken from it with the note ID, never with the case ID.
   Statements that are your own reasoning, not stated in the evidence, must be marked "(inference)".
3. Klipper reports text fault messages (e.g. "ADC out of range"), not numbered fault codes. Never invent codes, part numbers, pins, dates, causes or repairs.
4. Hardware matters: if a fix depends on a board, toolhead, probe or wiring variant, name the variant and say the user must check theirs.
   Each evidence item says which printer models it applies to ("any" = not specific to one printer). This knowledge base covers the Voron 2.4 (v2).
   Never transfer printer-specific values (pins, part counts, lengths, config values) to a printer the item does not apply to. If the question is about a printer or version the evidence does not cover, abstain as in rule 6 instead of answering from general knowledge.
5. Safety: never suggest disabling or bypassing thermal protection or verify_heater, and never suggest raising max_temp, max_power or similar limits without first verifying the hardware ratings. Recommend powering off before touching wiring.
   Safety reminders that come from these rules and not from the evidence go on their own line starting with "Safety:" and carry NO citation; never attach an evidence ID to a statement that evidence item does not make.
6. Abstain only if the question is not about Voron printers or Klipper, or no evidence item addresses it, or the evidence contradicts itself on the key point. Then start your reply with "{ABSTAIN_MARKER}" as plain text (no formatting) and say briefly what is missing and which measurements, logs or details would help.
   If the evidence addresses the question only partly, do NOT abstain: answer with what it supports and state the uncertainty.
7. Be concise: at most about 250 words.

Format:
Answer: <2-4 sentences>
Evidence:
- Official documentation: ... [ID]
- Community cases: ... [ID] (state the resolution status)
- Inference: ... (or "none")
What to check next: <1-3 concrete checks, with citations where possible>"""


HYDE_PROMPT = """Write a short passage (3-5 sentences) as it would appear in the official Voron 2.4 or Klipper documentation
and answers the user's question. Use the exact terms the docs would use: Klipper config section and option names,
error message text, part and board names. Do not mention the user or the question. No preamble."""


def hyde_query(question: str, chat: BaseChatModel, log: list | None = None) -> str:
    """HyDE: search with a hypothetical doc passage, so the query uses the docs' vocabulary.

    The question is kept in front: BM25 still needs its exact error strings, and a wrong guess
    in the passage cannot fully replace what the user actually asked. Only retrieval uses this;
    the answer prompt still gets the original question.
    """
    reply = chat.invoke([SystemMessage(HYDE_PROMPT), HumanMessage(question)])
    passage = reply.content if isinstance(reply.content, str) else str(reply.content)
    if log is not None:
        log.append({"question": question, "passage": passage, "usage": getattr(reply, "usage_metadata", None) or {}})
    return f"{question}\n\n{passage}"


def evidence_label(meta: dict) -> str:
    if meta.get("authority") != "community_case":
        return f"OFFICIAL DOCUMENTATION ({meta.get('publisher')})"
    status = meta["resolution_status"]
    if status in ("confirmed_owner_retest", "confirmed_owner"):
        kind = "OWNER-CONFIRMED"
    elif status == "unresolved":
        kind = "UNRESOLVED"
    else:
        kind = "UNCONFIRMED OR PARTIAL"
    return f"COMMUNITY CASE - {kind} (status: {status})"


def format_evidence(docs: list[Document], budget: int = EVIDENCE_TOKEN_BUDGET) -> tuple[str, list[Document]]:
    """Render evidence blocks in rank order until the token budget is reached. Returns (text, used docs)."""
    blocks, used, total = [], [], 0
    for doc in docs:
        meta = doc.metadata
        header = [f"[{meta['doc_id']}] {evidence_label(meta)}"]
        note = ""
        if meta.get("authority") == "community_case":
            header.append(f"Equipment: {meta['equipment_family']}; hardware: {meta['hardware_variant']}; date: {meta['date']}")
            header.append(f"Applies to: {', '.join(meta.get('printer_models') or ['any'])}")
            if meta.get("curator_note"):
                # Own ID, so the model can cite our guidance without attributing it to the case owner.
                note = f"\n\n[{meta['doc_id']}-note] {NOTE_LABEL}\n{meta['curator_note']}"
        else:
            header.append(f"Board variant: {meta.get('board_variant')}; docs commit: {meta.get('commit')}")
            header.append(f"Applies to: {', '.join(meta.get('printer_models') or ['any'])}")
        block = "\n".join(header) + "\n" + doc.page_content + note
        cost = count_tokens(block)
        if used and total + cost > budget:
            break  # always keep at least the best match, even if it alone exceeds the budget
        blocks.append(block)
        used.append(doc)
        total += cost
    return "\n\n---\n\n".join(blocks), used


# Models sometimes wrap the marker in Markdown (**...**) or put it after "Answer:" (seen in a real run).
_ABSTAIN = re.compile(r"^[\s*_#>`]*(Answer:)?[\s*_#>`]*" + re.escape(ABSTAIN_MARKER))
_BRACKETS = re.compile(r"\[([^\[\]]+)\]")
_ID = re.compile(r"^(C\d{2}(-note)?|D\d{2}[#~]\S+)$")


def cited_ids(text: str) -> list[str]:
    """IDs cited as [C08] or [C03, D01#adc-error], in order of first appearance."""
    found: list[str] = []
    for group in _BRACKETS.findall(text):
        for part in (p.strip() for p in group.split(",")):
            if _ID.match(part) and part not in found:
                found.append(part)
    return found


@dataclass
class AnswerResult:
    text: str
    evidence: list[Document]
    evidence_text: str
    cited: list[str]
    invalid_citations: list[str]
    abstained: bool
    usage: dict = field(default_factory=dict)
    seconds: float = 0.0


def sources_block(result: "AnswerResult") -> str:
    """Sources with URLs plus citation warnings, shown after the answer by the CLI and the chat server."""
    # URLs come from our metadata, never from the model, so they cannot be invented.
    by_id = {d.metadata["doc_id"]: d.metadata for d in result.evidence}
    valid = valid_citations(result)
    lines = []
    if valid:
        lines.append("\nSources:")
        for doc_id in valid:
            case_id = doc_id.removesuffix("-note")
            label = NOTE_LABEL if doc_id != case_id else evidence_label(by_id[doc_id])
            lines.append(f"  [{doc_id}] {label}\n      {by_id[case_id]['source_url']}")
    return "\n".join(lines + citation_warnings(result))


def valid_citations(result: "AnswerResult") -> list[str]:
    return [c for c in result.cited if c not in result.invalid_citations]


def citation_warnings(result: "AnswerResult") -> list[str]:
    lines = []
    if result.invalid_citations:
        lines.append(f"\nWARNING: cited IDs that were not in the evidence: {', '.join(result.invalid_citations)}")
    if not valid_citations(result) and not result.abstained:
        lines.append("\nWARNING: answer has no valid citations; treat it as unsupported.")
    return lines


def answer_question(
    question: str,
    retrieve: Callable[[str, int], list[Document]],
    chat: BaseChatModel,
    k: int = TOP_K,
    printer: str | None = None,
) -> AnswerResult:
    # Step 1: retrieval (one query embedding in production).
    evidence_text, used = format_evidence(retrieve(question, k))

    # Step 2: exactly one generation call.
    # The printer setting tells the model what "my printer" means when the question does not say.
    context = f"User printer: {printer}\n" if printer else ""
    messages = [
        SystemMessage(SYSTEM_PROMPT),
        HumanMessage(f"{context}Question: {question}\n\nEvidence:\n\n{evidence_text}"),
    ]
    start = time.perf_counter()
    reply = chat.invoke(messages)
    seconds = time.perf_counter() - start
    text = reply.content if isinstance(reply.content, str) else str(reply.content)

    # Step 3: check citations against what the model was actually given.
    allowed = {d.metadata["doc_id"] for d in used} | {f"{d.metadata['doc_id']}-note" for d in used if d.metadata.get("curator_note")}
    cited = cited_ids(text)
    return AnswerResult(
        text=text,
        evidence=used,
        evidence_text=evidence_text,
        cited=cited,
        invalid_citations=[c for c in cited if c not in allowed],
        abstained=bool(_ABSTAIN.match(text)),
        usage=getattr(reply, "usage_metadata", None) or {},
        seconds=seconds,
    )
