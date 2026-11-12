"""OpenAI-compatible HTTP front end so chat UIs (LibreChat) can use the voron-rag pipeline.

Only the latest user message is the question; history and system prompts from the UI are ignored,
so every reply is exactly what `voron-rag ask` would print (answer + sources).
"""

import hmac
import json
import re
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from voron_rag.answer import _BRACKETS, _ID, NOTE_LABEL, AnswerResult, citation_warnings, evidence_label, valid_citations

# Model name shown in the UI -> the user's printer (None = no printer setting).
MODELS = {"voron-rag": None, "voron-rag-v2": "v2"}


def section_slug(chunk_id: str) -> str:
    """D10#bed_mesh/4 -> bed_mesh-4: the viewer's section anchor for a doc chunk."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", chunk_id.partition("#")[2])


def viewer_url(base_url: str, cited_id: str) -> str:
    """Viewer page of the cited document, scrolled to and highlighting the cited chunk."""
    if cited_id.endswith("-note"):
        return f"{base_url}/source/{cited_id.removesuffix('-note')}#s-note"
    if "#" in cited_id:
        return f"{base_url}/source/{cited_id.partition('#')[0]}#s-{section_slug(cited_id)}"
    return f"{base_url}/source/{cited_id}#s-case"


def linked_reply(result: AnswerResult, base_url: str) -> str:
    """Answer with [D10#x] citations turned into numbered links [[1]](viewer), plus a numbered source list.

    Same format as the VPS rag-stub. Only valid citations get links; invalid ones stay plain text.
    """
    numbers = {cid: n for n, cid in enumerate(valid_citations(result), 1)}

    def link(match: re.Match) -> str:
        parts = [p.strip() for p in match.group(1).split(",")]
        if not any(_ID.match(p) for p in parts):
            return match.group(0)  # ordinary bracketed text, not a citation
        return " ".join(f"[[{numbers[p]}]]({viewer_url(base_url, p)})" if p in numbers else f"[{p}]" for p in parts)

    lines = [_BRACKETS.sub(link, result.text)]
    if numbers:
        by_id = {d.metadata["doc_id"]: d.metadata for d in result.evidence}
        lines.append("\n**Sources**\n")
        for cid, n in numbers.items():
            meta = by_id[cid.removesuffix("-note")]
            label = NOTE_LABEL if cid.endswith("-note") else evidence_label(meta)
            title = f": {meta['doc_title']}" if meta.get("doc_title") else ""
            lines.append(f"{n}. [{cid} - {label}{title}]({viewer_url(base_url, cid)}) · [Original]({meta['source_url']})")
    return "\n".join(lines + citation_warnings(result))


STREAM_CHUNK, STREAM_DELAY = 24, 0.02  # characters per SSE chunk, seconds between chunks (as in the VPS rag-stub)

PAGE_STYLE = """
:root { color-scheme: light dark; --fg: #1d1d1f; --muted: #666; --bg: #fff; --mark: #fff3b0; --bar: #e0a800; }
@media (prefers-color-scheme: dark) { :root { --fg: #e8e8e8; --muted: #9a9a9a; --bg: #161616; --mark: #4a3f10; --bar: #e0a800; } }
body { font-family: system-ui, sans-serif; color: var(--fg); background: var(--bg); max-width: 50rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
.meta { color: var(--muted); }
section { scroll-margin-top: 2rem; padding: .5rem 1rem; margin: .5rem 0; border-left: 4px solid transparent; }
section:target { background: var(--mark); border-left-color: var(--bar); }
.id { color: var(--muted); font-size: .8rem; font-family: ui-monospace, monospace; }
.text { white-space: pre-wrap; overflow-wrap: anywhere; margin: .25rem 0 0; font-family: inherit; }
"""


def _section(anchor: str, label: str, text: str) -> str:
    return (f'<section id="s-{escape(anchor)}"><div class="id">{escape(label)}</div>'
            f'<pre class="text">{escape(text)}</pre></section>')


def _page(title: str, meta: str, original: str, sections: list[str]) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{escape(title)}</title>
<style>{PAGE_STYLE}</style></head><body>
<h1>{escape(title)}</h1>
<p class="meta">{meta} &middot; <a href="{escape(original)}" target="_blank" rel="noopener">Original</a></p>
{chr(10).join(sections)}
</body></html>"""


def source_pages(corpus: list) -> dict[str, str]:
    """Viewer HTML per source (D10, C05, ...): every chunk is a section; the URL fragment highlights the cited one."""
    groups: dict[str, list] = {}
    for doc in corpus:  # corpus order = document order, so sections appear as in the original
        groups.setdefault(doc.id.partition("#")[0], []).append(doc)

    pages = {}
    for source, docs in groups.items():
        meta = docs[0].metadata
        if meta.get("authority") == "community_case":
            sections = [_section("case", f"[{source}] {evidence_label(meta)}", docs[0].page_content)]
            if meta.get("curator_note"):
                sections.append(_section("note", f"[{source}-note] {NOTE_LABEL}", meta["curator_note"]))
            info = escape(f"{evidence_label(meta)} · {meta.get('equipment_family', '')} · {meta.get('date', '')}")
            pages[source] = _page(f"Community case {source}", info, meta["source_url"], sections)
        else:
            sections = [_section(section_slug(d.id), d.id, d.page_content) for d in docs]
            info = escape(f"{evidence_label(meta)} · {meta.get('repo', '')} @ {meta.get('commit', '')} · {meta.get('license', '')}")
            pages[source] = _page(f"{source}: {meta.get('doc_title', source)}", info, meta["source_url"].partition("#")[0], sections)
    return pages


def last_user_text(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):  # OpenAI "content parts" form
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
            return content.strip() if isinstance(content, str) else ""
    return ""


def make_handler(answer: Callable[[str, str | None], str], api_key: str, pages: dict[str, str] | None = None):
    """answer(question, printer) -> reply text. API requests need `Authorization: Bearer <api_key>`.

    pages (source id -> HTML) are served at /source/<id> without a key: browsers open them from citation links.
    """
    pages = pages or {}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status: int, message: str) -> None:
            self._send(status, {"error": {"message": message, "type": "invalid_request_error"}})

        def _authorized(self) -> bool:
            given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if hmac.compare_digest(given.encode(), api_key.encode()):
                return True
            self._error(401, "invalid or missing API key")
            return False

        def do_GET(self) -> None:
            path = self.path.partition("?")[0]
            if path.startswith("/source/"):
                html = pages.get(path.removeprefix("/source/"))
                if html is None:
                    return self._error(404, "unknown document")
                data = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if not self._authorized():
                return
            if self.path.rstrip("/") != "/v1/models":
                return self._error(404, "not found")
            self._send(200, {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "voron-rag"} for m in MODELS]})

        def do_POST(self) -> None:
            if not self._authorized():
                return
            if self.path.rstrip("/") != "/v1/chat/completions":
                return self._error(404, "not found")
            try:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._error(400, "body must be JSON")
            model = request.get("model") if isinstance(request, dict) else None
            if model not in MODELS:
                return self._error(400, f"unknown model {model!r}; use one of {', '.join(MODELS)}")
            question = last_user_text(request.get("messages") or [])
            if not question:
                return self._error(400, "no user message with text")

            try:
                text = answer(question, MODELS[model])
            except Exception:
                traceback.print_exc()
                return self._send(500, {"error": {"message": "voron-rag failed; see server log", "type": "server_error"}})

            completion_id, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())
            if not request.get("stream"):
                return self._send(200, {
                    "id": completion_id, "object": "chat.completion", "created": created, "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                })

            # The answer is complete (and citation-checked) before streaming starts; the small chunks are presentation only.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            base = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model}

            def send(delta: dict, finish: str | None = None) -> None:
                choice = {"index": 0, "delta": delta, "finish_reason": finish}
                self.wfile.write(f"data: {json.dumps(base | {'choices': [choice]})}\n\n".encode())

            send({"role": "assistant", "content": ""})
            for i in range(0, len(text), STREAM_CHUNK):
                time.sleep(STREAM_DELAY)
                send({"content": text[i:i + STREAM_CHUNK]})
            send({}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")

    return Handler


def make_server(answer: Callable[[str, str | None], str], api_key: str, host: str, port: int,
                pages: dict[str, str] | None = None) -> ThreadingHTTPServer:
    # One thread per connection: browsers preconnect to the citation host and send nothing, which would
    # block a single-threaded server (LibreChat then times out). The lock keeps answering one question at a time.
    # shortcut: questions are serialized because embedded Qdrant is one client; drop the lock with a Qdrant server.
    lock = threading.Lock()

    def answer_one(question: str, printer: str | None) -> str:
        with lock:
            return answer(question, printer)

    handler = make_handler(answer_one, api_key, pages)
    handler.timeout = 60  # close connections that send nothing (seconds)
    return ThreadingHTTPServer((host, port), handler)


def serve(answer: Callable[[str, str | None], str], api_key: str, host: str, port: int, pages: dict[str, str] | None = None) -> None:
    server = make_server(answer, api_key, host, port, pages)
    print(f"voron-rag serving OpenAI-compatible API on http://{host}:{port}/v1 (models: {', '.join(MODELS)})")
    server.serve_forever()
