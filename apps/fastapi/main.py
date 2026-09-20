from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import httpx
import time
import asyncio
import json
import logging
import structlog
from contextlib import asynccontextmanager
from vault import load_secrets
from database import log_request, init_db
from agent import classify_ticket
from auth import check_key

LITELLM_BASE_URL = "http://litellm:4000"
PII_GATE_URL = "http://pii-gate:8000"

BUDGET_LIMIT_MESSAGE = "You have reached your AI-Test limit"

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

STYLE_PROMPT = (
    "Antworte präzise und direkt auf die Frage. "
    "Keine langen Einleitungen, keine Füllsätze, "
    "keine ungefragten Zusatzinformationen. "
    "Verfasse Antworten nur so lange wie nötig. "
    "Ausführlich nur auf ausdrückliche Nachfrage "
    "oder wenn es für die Korrektheit nötig ist."
)

def is_budget_error(status_code, content):
    try:
        return status_code in (400, 429) and b"budget" in content.lower()
    except Exception:
        return False


def budget_error_response():
    return JSONResponse(
        status_code=400,
        content={"error": {"message": BUDGET_LIMIT_MESSAGE, "type": "budget_exceeded", "code": "400"}},
    )


def budget_stream_response(model):
    base = {
        "id": "budget-block",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }

    async def gen():
        first = dict(base)
        first["choices"] = [{"index": 0, "delta": {"role": "assistant", "content": BUDGET_LIMIT_MESSAGE}, "finish_reason": None}]
        yield f"data: {json.dumps(first)}\n\n".encode()
        last = dict(base)
        last["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        yield f"data: {json.dumps(last)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_secrets()
    await init_db()
    yield


app = FastAPI(title="FastAPI Gateway", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(
        "unhandled_exception",
        method=request.method,
        path=request.url.path,
        error_type=type(exc).__name__,
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


class ClassifyRequest(BaseModel):
    message: str


@app.post("/agent/classify")
async def agent_classify(req: ClassifyRequest, _=Depends(check_key)):
    start = time.time()
    result = await classify_ticket(req.message)
    duration_ms = int((time.time() - start) * 1000)
    asyncio.create_task(
        log_request("gpt-4.1-mini", 0, 0, 200, duration_ms)
    )
    return result


@app.get("/gateway/health")
async def health():
    return {"status": "ok"}


@app.get("/gateway/stats")
async def stats(_=Depends(check_key)):
    # Usage metadata — requires the agent API key (x-api-key). Health stays open
    # for monitoring; stats does not, so it can't be scraped from the public edge.
    from database import get_stats
    return await get_stats()


async def shadow_scrub_messages(msgs: list) -> None:
    """Ruft pii-gate/scrub für jede User-Nachricht auf und loggt Treffer
    (nur Entity-Typen, nie Werte), ohne den Inhalt zu verändern. Shadow-Mode,
    siehe specs/pii-gate-chat-completions.md — Blocken folgt erst nach der
    Beobachtungsphase."""
    for msg in msgs:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        try:
            async with httpx.AsyncClient(timeout=10.0) as pii_client:
                resp = await pii_client.post(
                    f"{PII_GATE_URL}/scrub", json={"text": content})
                resp.raise_for_status()
                result = resp.json()
        except Exception as e:
            logger.error("pii_scrub_failed", error=str(e))
            continue
        if result["masked"] or result["shadow"]:
            logger.info(
                "pii_scrub_shadow",
                masked=result["masked"],
                shadow=result["shadow"],
            )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"])
async def proxy(request: Request, path: str):
    url = f"{LITELLM_BASE_URL}/{path}"
    headers = dict(request.headers)
    headers.pop("host", None)

    body = await request.body()
    start_time = time.time()

    is_stream = False
    model = "unknown"
    if request.method == "POST" and body:
        try:
            req_json = json.loads(body)
            is_stream = req_json.get("stream", False)
            model = req_json.get("model", "unknown")
            uid = request.headers.get("x-librechat-user-id")
            if uid and "chat/completions" in path:
                req_json["user"] = uid
                msgs = req_json.get("messages")
                if isinstance(msgs, list):
                    await shadow_scrub_messages(msgs)
                    msgs.insert(0, {"role": "system", "content": STYLE_PROMPT})
                body = json.dumps(req_json).encode()
                headers.pop("content-length", None)
        except Exception:
            pass

    if is_stream:
        client = httpx.AsyncClient(timeout=120.0)
        upstream = client.build_request(
            request.method,
            url,
            headers=headers,
            content=body,
            params=dict(request.query_params),
        )
        resp = await client.send(upstream, stream=True)

        if resp.status_code != 200:
            err = await resp.aread()
            await resp.aclose()
            await client.aclose()
            if is_budget_error(resp.status_code, err):
                return budget_stream_response(model)
            return Response(content=err, status_code=resp.status_code)

        async def stream_generator():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                content=body,
                params=dict(request.query_params),
            )
        except httpx.RequestError as e:
            logger.error("upstream_request_failed", error=str(e))
            raise HTTPException(status_code=502, detail="An error has occurred")
    duration_ms = int((time.time() - start_time) * 1000)

    if is_budget_error(resp.status_code, resp.content):
        return budget_error_response()

    if "chat/completions" in path and request.method == "POST":
        try:
            resp_json = resp.json()
            usage = resp_json.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            asyncio.create_task(log_request(model, prompt_tokens, completion_tokens, resp.status_code, duration_ms))
        except Exception:
            pass

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )
