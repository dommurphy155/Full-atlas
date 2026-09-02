"""FastAPI route handlers."""

from __future__ import annotations
import time
import uuid

from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import (
    get_force_default_model,
    LISTEN_HOST,
    LISTEN_PORT,
    MAX_RETRIES,
    MODEL_CONTEXT_WINDOW,
    PAYLOAD_DIR,
    PROVIDER,
    SAVE_PAYLOAD_FILES,
    get_chat_url,
    get_messages_url,
    get_default_model,
    get_logger,
)
from .proxy import ProxyCore
from .translation import prepare_chat_body, prepare_messages_body, openai_response_to_anthropic, openai_sse_to_anthropic_sse
from .utils import dumps, loads, request_id, ws_request_id
from . import prettylog as pl

log = get_logger(__name__)

router = APIRouter()

# Set by main.py during lifespan startup
proxy: Optional[ProxyCore] = None


@router.get("/")
async def root() -> Dict[str, Any]:
    assert proxy is not None
    return {
        "service": "OpenRouter Translation Proxy",
        "version": "1.1.0",
        "provider": PROVIDER,
        "default_model": get_default_model(),
        "force_default_model": get_force_default_model(),
        "endpoints": [
            "POST /v1/chat/completions",
            "POST /v1/messages",
            "GET  /v1/models",
            "GET  /health",
            "GET  /health/keys",
        ],
        "keys_loaded": proxy.pool.stats()["total"],
    }


@router.get("/health")
async def health() -> Dict[str, Any]:
    assert proxy is not None
    return {
        "status": "ok",
        "keys": proxy.pool.stats(),
        "listen": f"{LISTEN_HOST}:{LISTEN_PORT}",
    }


@router.get("/health/keys")
async def health_keys() -> Dict[str, Any]:
    """Detailed per-key statistics (no secret material)."""
    assert proxy is not None
    return {
        "status": "ok",
        "summary": proxy.pool.stats(),
        "keys": proxy.pool.detailed_stats(),
    }


@router.get("/stats")
async def stats() -> Dict[str, Any]:
    """Legacy /stats endpoint for atlas CLI compatibility."""
    assert proxy is not None
    stats = proxy.pool.stats()
    return {
        "total_keys": stats["total"],
        "healthy_keys": stats["healthy"],
        "cooling_keys": stats["cooling"],
        "suspended_keys": stats["suspended"],
    }




@router.get("/v1/models")
@router.get("/models")
async def models(request: Request) -> Response:
    """Return Anthropic-shaped model IDs so Claude Code UI accepts them.
    Actual inference always uses the provider's default model via get_force_default_model().
    """
    rid = request_id(request)
    now = int(time.time())
    default_model = get_default_model()
    data = {
        "object": "list",
        "data": [
            {
                "id": "claude-opus-5",
                "object": "model",
                "created": now,
                "owned_by": "anthropic",
                "display_name": "Opus 5",
                "type": "model",
                "context_window": MODEL_CONTEXT_WINDOW,
            },
            {
                "id": "claude-opus-5[1m]",
                "object": "model",
                "created": now,
                "owned_by": "anthropic",
                "display_name": "Opus 5 (1M context)",
                "type": "model",
                "context_window": MODEL_CONTEXT_WINDOW,
            },
            {
                "id": "claude-sonnet-5",
                "object": "model",
                "created": now,
                "owned_by": "anthropic",
                "display_name": "Sonnet 5",
                "type": "model",
                "context_window": MODEL_CONTEXT_WINDOW,
            },
            {
                "id": "claude-sonnet-5[1m]",
                "object": "model",
                "created": now,
                "owned_by": "anthropic",
                "display_name": "Sonnet 5 (1M context)",
                "type": "model",
                "context_window": MODEL_CONTEXT_WINDOW,
            },
            {
                "id": "claude-haiku-4-5",
                "object": "model",
                "created": now,
                "owned_by": "anthropic",
                "display_name": "Haiku 4.5",
                "type": "model",
                "context_window": MODEL_CONTEXT_WINDOW,
            },
            {
                "id": default_model,
                "object": "model",
                "created": now,
                "owned_by": "openrouter" if PROVIDER != "huggingface" else "huggingface",
                "display_name": default_model,
                "type": "model",
                "context_window": MODEL_CONTEXT_WINDOW,
            },
        ],
    }
    return JSONResponse(data, headers={"x-request-id": rid})


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(request: Request) -> Response:
    assert proxy is not None
    rid = request_id(request)
    try:
        body = loads(await request.body())
    except Exception:
        return JSONResponse(
            {
                "error": {
                    "message": "invalid json",
                    "type": "invalid_request_error",
                }
            },
            status_code=400,
            headers={"x-request-id": rid},
        )

    if not isinstance(body, dict):
        return JSONResponse(
            {
                "error": {
                    "message": "body must be object",
                    "type": "invalid_request_error",
                }
            },
            status_code=400,
            headers={"x-request-id": rid},
        )

    body = prepare_chat_body(body)
    stream = bool(body.get("stream", False))
    payload = dumps(body)

    _payload_path = _dump_payload(rid, "", body)

    tr = pl.trace(rid)  # logging-only lifecycle trace
    tr.start("openai", body.get("model"), "chat/completions", stream)

    resp = await proxy.forward(
        "POST",
        get_chat_url(),
        body=payload,
        stream=stream,
        request_id=rid,
    )
    return resp

@router.post("/v1/messages")
@router.post("/messages")
async def messages(request: Request) -> Response:
    assert proxy is not None
    rid = request_id(request)
    try:
        body = loads(await request.body())
    except Exception:
        return JSONResponse(
            {
                "error": {
                    "message": "invalid json",
                    "type": "invalid_request_error",
                }
            },
            status_code=400,
            headers={"x-request-id": rid},
        )

    if not isinstance(body, dict):
        return JSONResponse(
            {
                "error": {
                    "message": "body must be object",
                    "type": "invalid_request_error",
                }
            },
            status_code=400,
            headers={"x-request-id": rid},
        )

    body = prepare_messages_body(body) if PROVIDER != "huggingface" else prepare_chat_body(body)
    stream = bool(body.get("stream", False))
    payload = dumps(body)

    _payload_path = _dump_payload(rid, "_messages", body)

    tr = pl.trace(rid)  # logging-only lifecycle trace
    tr.start("anthropic", body.get("model"), "messages", stream)

    extra: Dict[str, str] = {}
    if "anthropic-version" in request.headers:
        extra["anthropic-version"] = request.headers["anthropic-version"]
    else:
        extra["anthropic-version"] = "2023-06-01"
    if "anthropic-beta" in request.headers:
        extra["anthropic-beta"] = request.headers["anthropic-beta"]

    upstream_url = get_chat_url() if PROVIDER == "huggingface" else get_messages_url()
    resp = await proxy.forward(
        "POST",
        upstream_url,
        body=payload,
        extra_headers=extra,
        stream=stream,
        request_id=rid,
    )

    # HF provider: convert OpenAI response → Anthropic /messages shape
    if PROVIDER == "huggingface":
        if not stream and isinstance(resp, Response):
            openai_data = loads(resp.body)
            anthropic_data = openai_response_to_anthropic(openai_data, rid=rid)
            new_body = dumps(anthropic_data)
            return Response(
                content=new_body,
                status_code=resp.status_code,
                media_type="application/json",
                headers={k: v for k, v in resp.headers.items() if k.lower() != "content-length"},
            )
        # Streaming: convert OpenAI SSE → Anthropic SSE
        if stream:
            return await _stream_openai_to_anthropic(
                rid, upstream_url, payload, resp
            )

    return resp



async def _stream_openai_to_anthropic(rid: str, upstream_url: str, payload: bytes, resp: Response) -> StreamingResponse:
    """Stream an OpenAI SSE response from HF and re-emit as Anthropic SSE."""
    async def translate_stream():
        # Yield message_start + content_block_start scaffolding
        yield _sse_event("message_start", {
            "type": "message_start",
            "message": {
                "id": rid,
                "type": "message",
                "role": "assistant",
                "model": get_default_model(),
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

        yield _sse_event("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        })

        # Stream from upstream (proxy.iter_upstream_sse or proxy.forward already returned a StreamingResponse)
        async for raw in resp.body_iterator:
            if not raw:
                continue
            frame = raw.strip()
            if not frame:
                continue
            data_line = None
            for line in frame.split(b"\n"):
                if line.startswith(b"data:"):
                    data_line = line[5:].strip()
                    break
            if data_line is None or data_line == b"[DONE]":
                continue
            try:
                chunk = loads(data_line)
            except Exception:
                continue

            # HF SSE per-chunk diagnostics (debug only — too noisy for INFO)
            try:
                log.debug(
                    "req=%s HF_SSE_CHUNK %s",
                    rid,
                    data_line.decode("utf-8", errors="replace")[:4000],
                )
            except Exception:
                pass

            # Emit Anthropic events for each OpenAI delta
            for evt_name, evt_data in openai_sse_to_anthropic_sse(chunk):
                if evt_data is None:
                    continue
                yield _sse_event(evt_name, evt_data)

        # Close events
        yield _sse_event("content_block_stop", {
            "type": "content_block_stop",
            "index": 0,
        })
        yield _sse_event("message_stop", {
            "type": "message_stop",
        })

    return StreamingResponse(
        translate_stream(),
        media_type="text/event-stream",
        headers={"x-request-id": rid},
    )


def _sse_event(event: str, data: dict) -> bytes:
    raw = dumps(data)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return f"event: {event}\ndata: {raw}\n\n".encode("utf-8")


def _dump_payload(rid: str, tag: str, body: Any) -> str:
    """Write the request payload to PAYLOAD_DIR when SAVE_PAYLOAD_FILES is on.

    Returns the path written ("" when disabled or on failure). Never raises.
    """
    if not SAVE_PAYLOAD_FILES:
        return ""
    import os
    import orjson

    os.makedirs(PAYLOAD_DIR, exist_ok=True)
    path = os.path.join(PAYLOAD_DIR, f"payload_{rid}{tag}.json")
    try:
        with open(path, "w") as f:
            f.write(orjson.dumps(body, option=orjson.OPT_INDENT_2).decode())
        log.info("req=%s payload_saved %s (%d bytes)", rid, path, os.path.getsize(path))
    except Exception:
        return ""
    return path


