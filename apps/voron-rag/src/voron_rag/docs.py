"""Official Markdown docs -> cleaned text -> heading sections -> chunks (LangChain Documents).

Vocabulary:
- document: one source file (D01..D11)
- section:  the text under one heading, with its heading path (H1 > H2 > H3)
- chunk:    what we embed and retrieve; one section, or a piece of a long one
"""

import re
from dataclasses import dataclass, field

import tiktoken
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from voron_rag.sources import SOURCES, Source

# Manifest target: 250-450 tokens per chunk. Upper bound only; short sections stay short.
MAX_CHUNK_TOKENS = 450

_ENC = tiktoken.get_encoding("cl100k_base")

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_TITLE = re.compile(r'^title:\s*"?(.*?)"?\s*$', re.MULTILINE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT = re.compile(r"<script\b.*?</script>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_PAGE_DIV = re.compile(r'^<div [^>]*id="([^"]+)"[^>]*markdown="1"')
_FENCE = "```"


def count_tokens(text: str) -> int:
    return len(_ENC.encode(text))


def slugify(heading: str) -> str:
    # shortcut: approximates the Jekyll/MkDocs anchor rules; spot-check links, fix per site if one breaks.
    slug = re.sub(r"[^\w\- ]", "", heading.lower()).strip()
    return re.sub(r"\s+", "-", slug)


# ---------- 1. Cleaning ----------


def clean_markdown(raw: str, drop_pages: tuple[str, ...] = ()) -> tuple[str, str]:
    """Return (title, cleaned markdown). HTML is removed only outside code fences."""
    title = ""
    if m := _FRONT_MATTER.match(raw):
        if t := _TITLE.search(m.group(1)):
            title = t.group(1)
        raw = raw[m.end() :]
    raw = _SCRIPT.sub("", _COMMENT.sub("", raw))

    out: list[str] = []
    in_code = False
    skipping = False
    for line in raw.splitlines():
        if line.strip().startswith(_FENCE):
            in_code = not in_code
        elif not in_code and (page := _PAGE_DIV.match(line)):
            # D05: one interactive page per <div id=...>; drop pages for other printer models.
            skipping = page.group(1).startswith(drop_pages) if drop_pages else False
        if skipping:
            continue
        if not in_code:
            line = _TAG.sub("", _IMAGE.sub(r"\1", line)).rstrip()
        out.append(line)

    # Collapse runs of blank lines left behind by removed HTML.
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return title, text


# ---------- 2. Sections ----------


@dataclass
class Section:
    headings: list[tuple[int, str]]  # (level, title) from the top heading down to this section
    body: str

    @property
    def title(self) -> str:
        return self.headings[-1][1] if self.headings else ""

    @property
    def level(self) -> int:
        return self.headings[-1][0] if self.headings else 0


def split_sections(text: str) -> list[Section]:
    """Split at headings, but never at '#' lines inside code (Klipper config comments)."""
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []  # (level, title) of the current heading ancestry
    lines: list[str] = []
    in_code = False

    def flush() -> None:
        body = "\n".join(lines).strip()
        if stack or body:
            sections.append(Section(list(stack), body))

    for line in text.splitlines():
        if line.strip().startswith(_FENCE):
            in_code = not in_code
        heading = None if in_code else _HEADING.match(line)
        if heading:
            flush()
            lines = []
            level, title = len(heading.group(1)), heading.group(2).replace("`", "").strip()
            # Pop siblings and deeper headings, then push this one.
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        else:
            lines.append(line)
    flush()
    return sections


def keep_section(section: Section, keep: tuple[str, ...]) -> bool:
    """With an allowlist, keep a section only if its level-2 ancestor starts with an allowed title."""
    if not keep:
        return True
    return any(level == 2 and title.startswith(keep) for level, title in section.headings)


# ---------- 3. Chunks ----------


def _blocks(body: str) -> list[str]:
    """Paragraphs and whole code blocks; blank lines inside code do not split."""
    blocks, current, in_code = [], [], False
    for line in body.splitlines():
        if line.strip().startswith(_FENCE):
            in_code = not in_code
        if not line.strip() and not in_code:
            if current:
                blocks.append("\n".join(current))
            current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _split_oversized_block(block: str, limit: int) -> list[str]:
    """Split a too-long block at line boundaries; code pieces are re-wrapped in fences."""
    is_code = block.lstrip().startswith(_FENCE)
    lines = block.splitlines()
    if is_code:
        lines = [ln for ln in lines if not ln.strip().startswith(_FENCE)]
    pieces, current = [], []
    for line in lines:
        if current and count_tokens("\n".join(current + [line])) > limit:
            pieces.append(current)
            current = []
        current.append(line)
    if current:
        pieces.append(current)
    wrap = (lambda p: f"{_FENCE}\n" + "\n".join(p) + f"\n{_FENCE}") if is_code else "\n".join
    return [wrap(p) for p in pieces]


def pack_blocks(body: str, limit: int = MAX_CHUNK_TOKENS) -> list[str]:
    """Greedily pack whole blocks into chunks of at most `limit` tokens."""
    chunks, current = [], []
    for block in _blocks(body):
        parts = [block] if count_tokens(block) <= limit else _split_oversized_block(block, limit)
        for part in parts:
            if current and count_tokens("\n\n".join(current + [part])) > limit:
                chunks.append("\n\n".join(current))
                current = []
            current.append(part)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


# Printer names as headings and users write them, e.g. "Inductive Probe Wiring (V1, Trident, V2, Switchwire)".
# V1/V2 followed by ".digit" is a board revision ("EBB36 v1.2", "Octopus Pro v2.0"), not a printer.
_PRINTERS = (
    ("v0", r"V0|Voron 0"), ("legacy", r"Legacy"), ("v1", r"V1(?!\.\d)|Voron 1"),
    ("trident", r"Trident"), ("v2", r"V?2\.4(?:r\d)?|V2(?!\.\d)|Voron 2"), ("switchwire", r"Switchwire"),
)
PRINTERS = tuple(name for name, _ in _PRINTERS)


def detect_printers(text: str) -> list[str]:
    """Printer models named in a heading path or a question."""
    return [name for name, pat in _PRINTERS if re.search(rf"\b(?:{pat})\b", text, re.IGNORECASE)]


def printer_models(heading_path: str, default: tuple[str, ...] = ("any",)) -> list[str]:
    """Printers a section applies to: those named in its heading path, else the source's default."""
    return detect_printers(heading_path) or list(default)


def target_printers(question: str, printer: str | None = None) -> list[str]:
    """The user's printer (a setting) plus every printer the question names; [] = unknown, so no filter."""
    return list(dict.fromkeys(([printer] if printer else []) + detect_printers(question)))


def _base_metadata(src: Source, title: str) -> dict:
    return {
        "source_id": src.source_id,
        "authority": "official_documentation",
        "source_type": "official_documentation",
        "publisher": "Klipper" if "klipper" in src.repo.lower() else "Voron Design",
        "doc_title": title,
        "repo": src.repo,
        "commit": src.commit[:7],
        "path": src.path,
        "board_variant": src.board_variant,
        "license": "GPL-3.0",
    }


def heading_chunks(src: Source, title: str, sections: list[Section]) -> list[Document]:
    docs, seen = [], {}
    for section in sections:
        if not section.body:
            continue  # heading with no text of its own
        anchor = slugify(section.title or title)
        # The same heading can repeat (D05 has three "Fine Tuning Z Height"): number repeats.
        seen[anchor] = seen.get(anchor, 0) + 1
        key = anchor if seen[anchor] == 1 else f"{anchor}~{seen[anchor]}"
        # Document title, then every heading below H1 (H1 usually repeats the title).
        heading_path = " > ".join([title, *(t for level, t in section.headings if level > 1)])
        for i, text in enumerate(pack_blocks(section.body), start=1):
            doc_id = f"{src.source_id}#{key}" + (f"/{i}" if i > 1 else "")
            meta = _base_metadata(src, title) | {
                "heading_path": heading_path,
                "printer_models": printer_models(heading_path, src.printers),
                "source_url": f"{src.url}#{anchor}",
                "chunker": "heading",
            }
            # The heading path goes into the searchable text: "set it to 0.6" means nothing without it.
            docs.append(Document(id=doc_id, page_content=f"{heading_path}\n\n{text}", metadata=meta))
    return docs


def recursive_chunks(src: Source, title: str, sections: list[Section]) -> list[Document]:
    """Comparison baseline: same filtered text, cut by size only, unaware of headings."""
    text = "\n\n".join(
        "#" * s.level + " " + s.title + "\n\n" + s.body if s.headings else s.body for s in sections
    )
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name="cl100k_base", chunk_size=MAX_CHUNK_TOKENS, chunk_overlap=0
    )
    meta = _base_metadata(src, title) | {
        "heading_path": title, "printer_models": printer_models(title, src.printers), "source_url": src.url, "chunker": "recursive",
    }
    return [
        Document(id=f"{src.source_id}~r{i}", page_content=piece, metadata=dict(meta))
        for i, piece in enumerate(splitter.split_text(text), start=1)
    ]


# ---------- 4. Whole corpus ----------


@dataclass
class DocsReport:
    sources: int = 0
    sections_total: int = 0
    sections_kept: int = 0
    chunks: int = 0
    tokens: int = 0
    max_chunk_tokens: int = 0
    failures: list[str] = field(default_factory=list)


def load_doc_chunks(chunker: str = "heading", sources: list[Source] = SOURCES) -> tuple[list[Document], DocsReport]:
    make = {"heading": heading_chunks, "recursive": recursive_chunks}[chunker]
    report = DocsReport()
    chunks: list[Document] = []
    for src in sources:
        if not src.local_path.exists():
            report.failures.append(f"{src.source_id}: file missing, run `voron-rag fetch-docs`")
            continue
        title, text = clean_markdown(src.local_path.read_text(encoding="utf-8"), src.drop_pages)
        sections = split_sections(text)
        kept = [s for s in sections if keep_section(s, src.keep_sections)]
        h1 = next((t for s in sections for level, t in s.headings if level == 1), "")
        title = src.title or title or h1 or src.source_id
        docs = make(src, title, kept)
        if not docs:
            report.failures.append(f"{src.source_id}: no chunks after filtering")
        report.sources += 1
        report.sections_total += len(sections)
        report.sections_kept += len(kept)
        chunks.extend(docs)

    sizes = [count_tokens(d.page_content) for d in chunks]
    report.chunks, report.tokens, report.max_chunk_tokens = len(chunks), sum(sizes), max(sizes, default=0)
    return chunks, report
