"""Dummy RAG backend for the LibreChat citations spike.

OpenAI-compatible /v1/chat/completions with canned German answers, plus
/source/<id> viewer pages that the citation links point to. Fictional data only.
"""
import asyncio
import json
import logging
import os
import time
import uuid
from html import escape

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("rag-stub")

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
STREAM_DELAY = 0.02  # seconds between chunks, so streaming is visible in the UI

# doc id -> (title, page, [(section id, heading, text)])
DOCS = {
    "FL-M200": ("Fehlerliste M-200", "S. 9", [
        ("213", "E-213", "Temperatur der Längssiegelung außerhalb des Sollbereichs. Maschine läuft mit Warnung weiter."),
        ("214", "E-214", "Anpressdruck der Quersiegelung außerhalb des Sollbereichs. Maschine stoppt im nächsten Takt."),
        ("215", "E-215", "Folienriss erkannt. Maschine stoppt sofort."),
    ]),
    "BA-M200-DE": ("Betriebsanleitung M-200 (DE)", "S. 47", [
        ("7.3.1", "7.3.1 Sicherheit", "Vor allen Arbeiten an der Quersiegelstation die Maschine spannungsfrei schalten und gegen Wiedereinschalten sichern."),
        ("7.3.2", "7.3.2 Siegelbacken prüfen", "Siegelbacken auf Verschmutzung und Verschleiß prüfen, bei Bedarf reinigen."),
        ("7.3.3", "7.3.3 Siegelbacken wechseln", "Nach jedem Backenwechsel ist der Anpressdruck neu einzustellen (siehe 7.3.5)."),
        ("7.3.4", "7.3.4 Pneumatik prüfen", "Schläuche und Verschraubungen der Quersiegelung auf Dichtheit prüfen."),
        ("7.3.5", "7.3.5 Anpressdruck einstellen", "Sollwert des Anpressdrucks: 4,2 bar ± 0,2 bar. Wert am Druckregler einstellen und am Manometer kontrollieren."),
        ("7.3.6", "7.3.6 Fehlerüberwachung", "Die Drucküberwachung mit Fehlermeldung E-214 ist ab Revision B verbaut."),
    ]),
    "SB-2025-0412": ("Servicebericht SB-2025-0412", "S. 1", [
        ("1", "Befund", "Maschine M-200 HS stoppt wiederholt mit E-214."),
        ("2", "Ursache", "Anpressdruck nach Backenwechsel nicht neu eingestellt (3,7 bar gemessen). Zusätzlich undichte Schlauchverschraubung an der Quersiegelung."),
        ("3", "Maßnahme", "Verschraubung getauscht, Anpressdruck auf 4,2 bar eingestellt, Probelauf ohne Fehler."),
    ]),
    "AR-M200": ("Anlagenregister M-200", "S. 1", [
        ("1", "SN 20-4470", "M-200 S, Revision A"),
        ("2", "SN 20-4471", "M-200 HS, Revision B"),
        ("3", "SN 20-4472", "M-200 HS, Revision C"),
    ]),
}

# Numbered citations used in the E-214 answer: (doc id, section id, label)
CITATIONS = [
    ("FL-M200", "214", "FL-M200, S. 9, E-214"),
    ("BA-M200-DE", "7.3.1", "BA-M200-DE, S. 47, 7.3.1"),
    ("BA-M200-DE", "7.3.5", "BA-M200-DE, S. 47, 7.3.5"),
    ("SB-2025-0412", "2", "SB-2025-0412, S. 1, Ursache"),
    ("AR-M200", "2", "AR-M200, S. 1, SN 20-4471"),
]

ABSTENTION = (
    "UNZUREICHENDE BELEGE: Zu dieser Frage habe ich in den verfügbaren Dokumenten "
    "keine passenden Stellen gefunden. Bitte nennen Sie z. B. eine Fehlermeldung "
    "oder Seriennummer."
)


def source_url(doc_id: str, section: str) -> str:
    return f"{PUBLIC_BASE_URL}/source/{doc_id}#s-{section}"


def cited_answer() -> str:
    c = {i + 1: f"[[{i + 1}]]({source_url(d, s)})" for i, (d, s, _) in enumerate(CITATIONS)}
    sources = "\n".join(
        f"{i + 1}. [{label}]({source_url(d, s)})" for i, (d, s, label) in enumerate(CITATIONS)
    )
    return f"""**E-214: Anpressdruck der Quersiegelung außerhalb des Sollbereichs**

Die Anlage SN 20-4471 ist eine M-200 HS in Revision B {c[5]}. E-214 bedeutet, dass der Anpressdruck der Quersiegelung außerhalb des Sollbereichs liegt; die Maschine stoppt im nächsten Takt {c[1]}.

**Maßnahmen**

- Vorher die Station spannungsfrei schalten und sichern {c[2]}.
- Anpressdruck am Druckregler prüfen und auf den Sollwert 4,2 bar ± 0,2 bar einstellen {c[3]}.
- Schläuche und Verschraubungen der Quersiegelung auf Dichtheit prüfen {c[4]}.

**Ähnlicher Servicefall**

Bei SB-2025-0412 war der Anpressdruck nach einem Backenwechsel nicht neu eingestellt (3,7 bar), zusätzlich war eine Schlauchverschraubung undicht {c[4]}.

**Quellen**

{sources}
"""


def message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # OpenAI content parts
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def pick_answer(messages: list) -> str:
    last_user = next((m for m in reversed(messages) if isinstance(m, dict) and m.get("role") == "user"), {})
    return cited_answer() if "E-214" in message_text(last_user.get("content")).upper() else ABSTENTION


def chunks(text: str, size: int = 24):
    for i in range(0, len(text), size):
        yield text[i:i + size]


app = FastAPI()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "invalid JSON")
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise HTTPException(400, "messages must be a list")
    messages = body["messages"]
    stream = bool(body.get("stream"))
    model = str(body.get("model", "rag-demo"))
    # Structure only, never message content.
    log.info(json.dumps({
        "event": "request",
        "model": model,
        "stream": stream,
        "message_count": len(messages),
        "roles": [m.get("role") if isinstance(m, dict) else None for m in messages],
        "fields": sorted(body.keys()),
    }))

    answer = pick_answer(messages)
    cid, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())

    if not stream:
        return JSONResponse({
            "id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def event(delta: dict, finish=None) -> str:
        chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    async def gen():
        yield event({"role": "assistant", "content": ""})
        for piece in chunks(answer):
            await asyncio.sleep(STREAM_DELAY)
            yield event({"content": piece})
        yield event({}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/source/{doc_id}", response_class=HTMLResponse)
def source(doc_id: str):
    if doc_id not in DOCS:
        raise HTTPException(404, "unknown document")
    title, page, sections = DOCS[doc_id]
    body = "\n".join(
        f'<section id="s-{escape(sid)}"><h2>{escape(head)}</h2><p>{escape(text)}</p></section>'
        for sid, head, text in sections
    )
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(doc_id)}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 46rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
section {{ scroll-margin-top: 2rem; padding: .5rem 1rem; border-left: 4px solid transparent; }}
section:target {{ background: #fff3b0; border-left-color: #e0a800; }}
.meta {{ color: #666; }}
</style></head><body>
<h1>{escape(title)}</h1>
<p class="meta">{escape(doc_id)} &middot; {escape(page)} &middot; fiktives Demo-Dokument</p>
{body}
</body></html>"""
