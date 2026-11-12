"""Load and validate the curated Voron community case summaries (JSONL)."""

import json
from pathlib import Path

from langchain_core.documents import Document

from voron_rag.docs import detect_printers

# Resolved from this file (src/voron_rag/cases.py), so it works from any working directory.
CASES_PATH = Path(__file__).resolve().parents[2] / "data/raw/voron24_cases.jsonl"

# Without these a case cannot be cited, trusted or matched to an error.
REQUIRED_FIELDS = (
    "case_id",
    "source_url",
    "source_type",
    "equipment_family",
    "error_signature",
    "symptoms",
    "diagnosis",
    "resolution_status",
)

# Strongest to weakest evidence. An unknown value is an error, never a silent default.
RESOLUTION_STATUSES = (
    "confirmed_owner_retest",
    "confirmed_owner",
    "moderator_diagnosis_acknowledged",
    "tentative_owner_report",
    "partial_improvement",
    "unresolved",
)


def read_cases(path: Path) -> list[dict]:
    """Read one case per line and return them in file order.

    Skips blank lines. Raises ValueError whose message contains "line <n>" for:
    malformed JSON, a line that is not a JSON object, a missing or empty required field,
    an unknown resolution_status, or a case_id already seen earlier in the file.
    """
    cases: list[dict] = []
    seen_ids: set[str] = set()  # IDs from earlier lines, for duplicate detection

    # "with" closes the file even if we raise halfway through.
    # Explicit utf-8: the default encoding differs between Windows and Linux.
    with path.open(encoding="utf-8") as file:
        # enumerate yields (number, line); start=1 matches how editors count lines.
        for line_no, line in enumerate(file, start=1):
            # 1. Blank or whitespace-only lines (often a trailing newline) are not records.
            if not line.strip():
                continue

            # 2. Parse one JSON value. Re-raise with the line number so the error
            #    says where to look; "from e" keeps the original parser error attached.
            try:
                case = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"line {line_no}: invalid JSON: {e}") from e

            # 3. Valid JSON is not necessarily a case: [1, 2] or "text" parse fine.
            if not isinstance(case, dict):
                raise ValueError(f"line {line_no}: expected a JSON object, got {type(case).__name__}")

            # 4. A missing key and an empty/whitespace value are equally useless as evidence.
            #    .get(name, "") turns a missing key into "", str() handles non-string values.
            for name in REQUIRED_FIELDS:
                if not str(case.get(name, "")).strip():
                    raise ValueError(f"line {line_no}: missing or empty required field {name!r}")

            # 5. Only known statuses: a typo like "fixed" must not pass as a confirmed repair.
            #    Safe to index directly because step 4 guaranteed the key exists.
            status = case["resolution_status"]
            if status not in RESOLUTION_STATUSES:
                raise ValueError(f"line {line_no}: unknown resolution_status {status!r}")

            # 6. case_id is the stable identity used for citations and, later, index IDs.
            case_id = case["case_id"]
            if case_id in seen_ids:
                raise ValueError(f"line {line_no}: duplicate case_id {case_id!r}")
            seen_ids.add(case_id)

            cases.append(case)

    return cases


# Curator glossary: brand/part names in hardware fields -> the generic role a user would type
# ("toolhead board", "Z probe"). Closes vocabulary gaps like "toolhead board" vs "SB2209" (M11).
HARDWARE_ROLES = {
    "SB2209": "CAN toolhead board",
    "EB2209": "CAN toolhead board",
    "Octopus": "controller board (MCU)",
    "Stealthburner": "toolhead",
    "Afterburner": "toolhead",
    "Clockwork 2": "extruder",
    "Galileo": "extruder",
    "Dragon High Flow": "hotend",
    "Tap V2": "nozzle-contact Z probe",
    "Klicky": "dockable Z probe",
    "Cartographer": "eddy-current Z probe",
}


def hardware_roles(hardware: str) -> str:
    """'SB2209 = CAN toolhead board; ...' for the glossary terms in `hardware`, or '' if none match."""
    return "; ".join(f"{term} = {role}" for term, role in HARDWARE_ROLES.items() if term.lower() in hardware.lower())


def case_to_document(case: dict) -> Document:
    """Turn one validated case into a LangChain Document.

    page_content is what search and embeddings see; metadata is carried along
    for citation, filtering and trust labeling, but is not searched.
    """
    # Optional fields get an explicit "unknown": a missing key would look like "not recorded
    # by accident", and "unknown" is a value we can display and filter on.
    hardware = case.get("hardware_variant") or "unknown"

    # Labeled lines, built only from structured fields, so the text is reproducible
    # and free of the curator's instructions that live in retrieval_text.
    page_content = "\n".join(
        [
            f"Community case {case['case_id']} ({case['equipment_family']})",
            f"Error: {case['error_signature']}",
            f"Symptoms: {case['symptoms']}",
            f"Diagnosis: {case['diagnosis']}",
            f"Resolution status: {case['resolution_status']}",
            f"Hardware: {hardware}",
        ]
    )
    if roles := hardware_roles(hardware):
        # Labeled as curator text: it is our classification, not part of the owner's report.
        page_content += f"\nHardware roles (curator glossary): {roles}"

    metadata = {
        # Identity and provenance: needed for every citation.
        "source_id": case["case_id"],
        "source_url": case["source_url"],
        "source_type": case["source_type"],  # kept exactly as in the data
        # Authority level: our own coarse label; official docs will get "official_documentation".
        "authority": "community_case",
        # Trust and scope: shown to the model and usable as filters.
        "resolution_status": case["resolution_status"],
        "equipment_family": case["equipment_family"],
        "printer_models": detect_printers(case["equipment_family"]) or ["any"],
        "hardware_variant": hardware,
        "topic": case.get("topic") or "unknown",
        "date": case.get("date") or "unknown",
        "error_signature": case["error_signature"],
        # Curator guidance (e.g. "do not claim this was a full repair"); for the answer step, not for search.
        "curator_note": case.get("retrieval_text") or "",
    }

    # id = case_id gives the same Document the same identity on every run;
    # in Milestone 5 this is what prevents duplicates in the vector store.
    return Document(id=case["case_id"], page_content=page_content, metadata=metadata)


def load_case_documents(path: Path = CASES_PATH) -> list[Document]:
    return [case_to_document(case) for case in read_cases(path)]
