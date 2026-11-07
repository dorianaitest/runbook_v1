"""Lokales Dev-Tool (NICHT im Container): zerlegt die EU-AI-Act-Markdown-Datei
in strukturierte Chunks (Erwaegungsgruende, Artikel, Anhaenge) und schreibt sie
nach data/aiact_chunks.jsonl. Diese JSONL wird committet; der Container liest
nur die JSONL und braucht daher keinen Parser.

Nutzung (lokal):  python build_aiact_chunks.py <pfad-zur-md>
"""
import json
import re
import sys
from pathlib import Path

MAX_CHARS = 1400   # Ziel-Chunkgroesse
OVERLAP = 200      # Ueberlappung beim Sub-Splitten langer Abschnitte

RE_ARTICLE = re.compile(r"^#{1,4}\s+_Article\s+(\d+)_")
RE_ANNEX = re.compile(r"^#{1,4}\s+\*\*ANNEX\s+([IVXL]+)\*\*")
RE_HEADING = re.compile(r"^#{1,4}\s+")
RE_RECITAL = re.compile(r"^\((\d+)\)\s*(.*)")

NOISE = re.compile(
    r"^(Regulation - EU|15\.\d{2}\.\d{2},|\d+/92\s*$|https?://eur-lex|"
    r"In force:|PE/\d+/|_OJ L,|ELI:)"
)


def clean_line(line: str) -> str:
    """Entfernt Markdown-/HTML-Deko aus einer Zeile."""
    line = re.sub(r"<sup>.*?</sup>", "", line)
    line = re.sub(r"</?(u|br|sub)\s*/?>", " ", line)
    line = line.replace("**", "").replace("<br>", " ")
    line = re.sub(r"(?<!\w)_(?!\w)", "", line)  # betonende Unterstriche
    line = re.sub(r"^\s*-\s+", "", line)         # Bullet
    return line.strip()


def load_clean_lines(md_path: str) -> list[str]:
    raw = Path(md_path).read_text(encoding="utf-8").split("\n")
    out, in_picture = [], False
    for ln in raw:
        if "<!-- Start of picture text -->" in ln:
            in_picture = True
            continue
        if "<!-- End of picture text -->" in ln:
            in_picture = False
            continue
        if in_picture or ln.strip().startswith("<!--"):
            continue
        if NOISE.match(ln.strip()):
            continue
        out.append(ln)
    return out


def norm(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("—", "-").replace("–", "-").replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def slug(section: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", section.lower()).strip("-")


def window(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    parts, i = [], 0
    while i < len(text):
        end = min(i + MAX_CHARS, len(text))
        if end < len(text):
            dot = text.rfind(". ", i + MAX_CHARS - 400, end)
            if dot != -1:
                end = dot + 1
        parts.append(text[i:end].strip())
        if end >= len(text):
            break
        i = end - OVERLAP
    return [p for p in parts if p]


def build(md_path: str) -> list[dict]:
    lines = load_clean_lines(md_path)

    adopted = next(
        (i for i, l in enumerate(lines) if "HAVE ADOPTED THIS REGULATION" in l),
        len(lines),
    )

    sections: list[tuple[str, str]] = []

    # --- Erwaegungsgruende (Praeambel) ---
    for ln in lines[:adopted]:
        m = RE_RECITAL.match(clean_line(ln))
        if m:
            sections.append((f"Recital {m.group(1)}", m.group(2)))

    # --- Artikel + Anhaenge (ueber Ueberschriften segmentiert) ---
    cur_section, buf, title_pending = None, [], False
    for ln in lines[adopted:]:
        m_art = RE_ARTICLE.match(ln)
        m_annex = RE_ANNEX.match(ln)
        if m_art or m_annex:
            if cur_section and buf:
                sections.append((cur_section, " ".join(buf)))
            cur_section = (
                f"Article {m_art.group(1)}" if m_art else f"ANNEX {m_annex.group(1)}"
            )
            buf, title_pending = [], True
            continue
        if RE_HEADING.match(ln):
            # Titelzeile direkt nach Artikel-/Annex-Ueberschrift mitnehmen,
            # sonstige Ueberschriften (CHAPTER/SECTION) als Trenner ignorieren.
            cleaned = clean_line(ln)
            if title_pending and cur_section and cleaned:
                buf.append(cleaned)
                title_pending = False
            continue
        title_pending = False
        cleaned = clean_line(ln)
        if cur_section and cleaned:
            buf.append(cleaned)
    if cur_section and buf:
        sections.append((cur_section, " ".join(buf)))

    chunks = []
    for section, raw in sections:
        text = norm(f"{section}. {raw}")
        if len(text) < 40:
            continue
        for n, part in enumerate(window(text), 1):
            chunks.append({"id": f"{slug(section)}-{n}", "section": section, "text": part})
    return chunks


def main():
    md_path = sys.argv[1] if len(sys.argv) > 1 else "Regulation - EU - 2024_1689 - EN - EUR-Lex.md"
    chunks = build(md_path)
    out = Path(__file__).parent / "aiact_chunks.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"{len(chunks)} chunks -> {out.name}")
    from collections import Counter
    kinds = Counter(c["section"].split()[0] for c in chunks)
    print("nach Typ:", dict(kinds))


if __name__ == "__main__":
    main()
