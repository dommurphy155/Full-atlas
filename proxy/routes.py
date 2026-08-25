"""FastAPI route handlers."""

from __future__ import annotations
import time
import re
import uuid

from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from .config import (
    DEBUG_CODEX_BODY,
    get_force_default_model,
    LISTEN_HOST,
    LISTEN_PORT,
    MAX_RETRIES,
    MODEL_CONTEXT_WINDOW,
    OPENROUTER_RESPONSES,
    PAYLOAD_DIR,
    PROMOTE_TEXT_TO_TOOLS,
    SAVE_PAYLOAD_FILES,
    PROVIDER,
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


def _extract_text_tool_calls(text: str):
    """Pull pseudo tool-calls (JSON / XML / freestyle) out of model text.

    Free models (esp. Nemotron) emit many shapes instead of native tool_calls.
    Returns (cleaned_text, list_of_{name,arguments,raw}).
    """
    import json as _json
    import re as _re

    tools = []

    def _norm_name(n: str) -> str:
        n = (n or "").strip()
        # Codex registers: exec, wait, request_user_input, collaboration
        # Map common free-model aliases TO those names (not the other way).
        aliases = {
            "exec_command": "exec",
            "shell": "exec",
            "bash": "exec",
            "run": "exec",
            "run_terminal_cmd": "exec",
            "run_command": "exec",
            "execute": "exec",
            "functions.exec": "exec",
            "function.exec": "exec",
            "local_shell": "exec",
        }
        return aliases.get(n.lower(), n)

    def _params_to_args(params: dict) -> str:
        if "command" in params and "cmd" not in params:
            params["cmd"] = params.pop("command")
        if "cmd" in params and isinstance(params["cmd"], str):
            c = params["cmd"].strip()
            if len(c) >= 2 and c[0] == c[-1] and c[0] in ("'", '"'):
                params["cmd"] = c[1:-1]
        try:
            return _json.dumps(params)
        except Exception:
            return str(params)

    def _add(name: str, params: dict, raw: str) -> None:
        name = _norm_name(name)
        if not name:
            return
        tools.append({"name": name, "arguments": _params_to_args(params), "raw": raw})

    def _parse_call_args(argstr: str) -> dict:
        params = {}
        for m in _re.finditer(
            r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(?:\"((?:\\.|[^\"])*)\"|'((?:\\.|[^'])*)'|([^,\s)]+))",
            argstr,
        ):
            k = m.group(1)
            v = m.group(2) if m.group(2) is not None else (
                m.group(3) if m.group(3) is not None else m.group(4)
            )
            if v is not None:
                params[k] = v
        return params

    # 1. <function=NAME> ... </function>
    xml_pat = _re.compile(
        r"<function\s*=\s*([a-zA-Z0-9_\-]+)>(.*?)</function>",
        _re.DOTALL | _re.IGNORECASE,
    )
    param_pat = _re.compile(
        r"<parameter\s*=\s*([a-zA-Z0-9_\-]+)>(.*?)</parameter>",
        _re.DOTALL | _re.IGNORECASE,
    )

    def _xml_repl(match):
        name = match.group(1).strip()
        body = match.group(2)
        params = {pm.group(1).strip(): pm.group(2).strip() for pm in param_pat.finditer(body)}
        _add(name, params, match.group(0))
        return ""

    text = xml_pat.sub(_xml_repl, text)

    # 2. <tool_call><invoke name="N">...</invoke></tool_call>
    invoke_pat = _re.compile(
        r"<tool_call>\s*<invoke\s+name=\"([^\"]+)\">(.*?)</invoke>\s*</tool_call>",
        _re.DOTALL | _re.IGNORECASE,
    )
    inv_param = _re.compile(
        r"<parameter\s+name=\"([^\"]+)\">(.*?)</parameter>",
        _re.DOTALL | _re.IGNORECASE,
    )

    def _inv_repl(match):
        name = match.group(1).strip()
        body = match.group(2)
        params = {pm.group(1).strip(): pm.group(2).strip() for pm in inv_param.finditer(body)}
        _add(name, params, match.group(0))
        return ""

    text = invoke_pat.sub(_inv_repl, text)

    # 3. Freestyle Nemotron block:
    # <tool_call>
    # FUNCTION
    # exec_command(cmd="...", timeout=5000)
    # </tool_call>
    freestyle_pat = _re.compile(
        r"<tool_call>\s*(?:FUNCTION\s*)?([a-zA-Z0-9_\.\-]+)\s*\((.*?)\)\s*(?:</tool_call>)?",
        _re.DOTALL | _re.IGNORECASE,
    )

    def _fs_repl(match):
        name = match.group(1).strip()
        argstr = match.group(2) or ""
        params = _parse_call_args(argstr)
        if not params and argstr.strip():
            params = {"cmd": argstr.strip().strip('"').strip("'")}
        _add(name, params, match.group(0))
        return ""

    text = freestyle_pat.sub(_fs_repl, text)

    # 4. Bare: exec_command(cmd="...") / Functions.exec(cmd="...")
    bare_pat = _re.compile(
        r"(?:^|\n)\s*(?:Functions\.)?([a-zA-Z][a-zA-Z0-9_\-]+)\s*\(([^)\n]*)\)\s*(?:\n|$)",
        _re.IGNORECASE,
    )
    _KNOWN = {
        "exec_command", "exec", "shell", "bash", "run", "run_terminal_cmd",
        "run_command", "execute", "read_file", "write_file", "apply_patch",
        "grep", "search", "list_dir", "glob",
    }

    def _bare_repl(match):
        name = match.group(1).strip()
        if name.lower() not in _KNOWN and not name.lower().startswith("exec"):
            return match.group(0)
        argstr = match.group(2) or ""
        params = _parse_call_args(argstr)
        if not params and argstr.strip():
            params = {"cmd": argstr.strip().strip('"').strip("'")}
        _add(name, params, match.group(0))
        return "\n"

    text = bare_pat.sub(_bare_repl, text)

    # 5. Functions.exec: {"cmd": "..."}
    colon_pat = _re.compile(
        r"(?:Functions\.)?([a-zA-Z][a-zA-Z0-9_\-]+)\s*:\s*(\{[^{}]*\})",
        _re.IGNORECASE,
    )

    def _colon_repl(match):
        name = match.group(1).strip()
        if name.lower() not in _KNOWN and not name.lower().startswith("exec"):
            return match.group(0)
        try:
            params = _json.loads(match.group(2))
            if not isinstance(params, dict):
                params = {"cmd": str(params)}
        except Exception:
            params = {"cmd": match.group(2)}
        _add(name, params, match.group(0))
        return ""

    text = colon_pat.sub(_colon_repl, text)

    # 6. JSON brace scanner
    cleaned_parts = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            j = text.find("{", i)
            if j < 0:
                cleaned_parts.append(text[i:])
                break
            cleaned_parts.append(text[i:j])
            i = j
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
            j += 1
        candidate = text[i:j]
        parsed = None
        try:
            parsed = _json.loads(candidate)
        except Exception:
            cleaned_parts.append(candidate)
            i = j
            continue
        if not isinstance(parsed, dict):
            cleaned_parts.append(candidate)
            i = j
            continue

        name = None
        args = None
        if "tool" in parsed and isinstance(parsed["tool"], str):
            name = parsed["tool"]
            args = {k: v for k, v in parsed.items() if k not in ("tool", "commentary", "type")}
            if not args and "cmd" in parsed:
                args = {"cmd": parsed["cmd"]}
        elif "name" in parsed and ("arguments" in parsed or "parameters" in parsed):
            name = parsed["name"]
            args = parsed.get("arguments") or parsed.get("parameters") or {}
        elif parsed.get("type") in ("exec_command", "shell", "function_call", "tool_call"):
            name = parsed.get("name") or parsed.get("type")
            args = {k: v for k, v in parsed.items() if k not in ("type", "name", "commentary")}
        elif set(parsed.keys()) <= {"commentary", "message", "status"}:
            cleaned_parts.append(candidate)
            i = j
            continue

        if name:
            if isinstance(args, dict):
                _add(name, args, candidate)
            else:
                if not isinstance(args, str):
                    try:
                        args = _json.dumps(args if args is not None else {})
                    except Exception:
                        args = str(args)
                tools.append({"name": _norm_name(name), "arguments": args, "raw": candidate})
        else:
            cleaned_parts.append(candidate)
        i = j

    cleaned = "".join(cleaned_parts)
    cleaned = _re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, tools


def _promote_text_tools_in_chat_response(resp: Response) -> Response:
    """
    Non-streaming: if the OpenAI chat/completions response contains assistant
    text with embedded pseudo tool-calls (XML/JSON/freestyle), promote them to
    real tool_calls and return a new Response with the fixed body.

    This brings /v1/chat/completions parity with /v1/responses, which already
    applies _extract_text_tool_calls in its streaming path.

    Controlled by PROMOTE_TEXT_TO_TOOLS — disable for frontier models that
    reliably emit native tool_calls and may produce JSON prose.
    """
    if not PROMOTE_TEXT_TO_TOOLS:
        return resp
    if resp.media_type and "application/json" not in resp.media_type:
        return resp
    try:
        data = loads(resp.body)
    except Exception:
        return resp
    if not isinstance(data, dict):
        return resp
    choices = data.get("choices") or []
    if not isinstance(choices, list):
        return resp
    changed = False
    for ch in choices:
        if not isinstance(ch, dict):
            continue
        msg = ch.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        # Skip if the model already returned native tool_calls — no need
        if msg.get("tool_calls"):
            continue
        cleaned, tools = _extract_text_tool_calls(content)
        if tools:
            msg["content"] = cleaned.strip() or None
            msg["tool_calls"] = [
                {
                    "id": f"call_{idx}_{t['name']}",
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "arguments": t["arguments"],
                    },
                }
                for idx, t in enumerate(tools)
            ]
            if msg["content"] is None:
                msg.pop("content", None)
            changed = True
    if changed:
        new_body = dumps(data)
        return Response(
            content=new_body,
            status_code=resp.status_code,
            # Filter content-length since body size changed
            headers={k: v for k, v in resp.headers.items() if k.lower() != "content-length"},
            media_type=resp.media_type,
        )
    return resp

def _promote_text_tools_in_messages_response(resp: Response) -> Response:
    """
    Non-streaming: for Anthropic /messages responses, if the assistant's
    content contains text blocks with embedded pseudo tool-calls, promote
    them to proper content blocks (text + tool_use) and re-serialize.
    """
    if not PROMOTE_TEXT_TO_TOOLS:
        return resp
    if resp.media_type and "application/json" not in resp.media_type:
        return resp
    try:
        data = loads(resp.body)
    except Exception:
        return resp
    if not isinstance(data, dict):
        return resp
    content = data.get("content")
    if not isinstance(content, list):
        return resp
    changed = False
    new_content: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            new_content.append(block)
            continue
        if block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                cleaned, tools = _extract_text_tool_calls(text)
                if tools:
                    changed = True
                    if cleaned.strip():
                        new_content.append({"type": "text", "text": cleaned.strip()})
                    for idx, t in enumerate(tools):
                        new_content.append({
                            "type": "tool_use",
                            "id": f"toolu_{uuid.uuid4().hex[:12]}",
                            "name": t["name"],
                            "input": t["arguments"],
                        })
                    continue
        new_content.append(block)
    if changed:
        data["content"] = new_content
        new_body = dumps(data)
        return Response(
            content=new_body,
            status_code=resp.status_code,
            # Filter content-length since body size changed
            headers={k: v for k, v in resp.headers.items() if k.lower() != "content-length"},
            media_type=resp.media_type,
        )
    return resp


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
            "POST /v1/responses",
            "WS   /v1/responses",
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

    # --- Payload logging for inspection ---
    _payload_path = _dump_payload(rid, "", body)
    # --- end payload logging ---

    tr = pl.trace(rid)  # logging-only lifecycle trace
    tr.start("openai", body.get("model"), "chat/completions", stream, payload_path=_payload_path)

    resp = await proxy.forward(
        "POST",
        get_chat_url(),
        body=payload,
        stream=stream,
        request_id=rid,
    )
    if not stream and isinstance(resp, Response):
        return _promote_text_tools_in_chat_response(resp)
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

    # --- Payload logging for inspection ---
    _payload_path = _dump_payload(rid, "_messages", body)
    # --- end payload logging ---

    tr = pl.trace(rid)  # logging-only lifecycle trace
    tr.start("anthropic", body.get("model"), "messages", stream, payload_path=_payload_path)

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

    if not stream and isinstance(resp, Response):
        return _promote_text_tools_in_messages_response(resp)
    return resp


@router.post("/v1/responses")
async def responses(request: Request) -> Response:
    """
    OpenAI Responses API → best-effort map to OpenRouter chat/completions.
    Translates `input` / `instructions` into messages when needed.
    Also translates the streaming response from Chat Completions SSE
    to Responses API SSE format.
    """
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

    # DEBUG: dump Codex request shape (gated behind ATLAS_DEBUG_CODEX_BODY)
    if DEBUG_CODEX_BODY:
        try:
            import json as _j
            _dbg = {
                "keys": sorted(body.keys()),
                "model": body.get("model"),
                "stream": body.get("stream"),
                "tools_count": len(body.get("tools") or []),
                "tool_names": [
                    (t.get("function") or t).get("name") if isinstance(t, dict) else None
                    for t in (body.get("tools") or [])[:20]
                ],
                "input_types": [
                    (it.get("type") if isinstance(it, dict) else type(it).__name__)
                    for it in (body.get("input") or [])[:30]
                ] if isinstance(body.get("input"), list) else type(body.get("input")).__name__,
                "has_instructions": "instructions" in body,
            }
            log.info("req=%s CODEX_BODY %s", rid, _j.dumps(_dbg)[:2000])
        except Exception as _e:
            log.warning("req=%s CODEX_BODY dump failed: %s", rid, _e)

    if "messages" not in body and "input" in body:
        inp = body.pop("input")
        messages_list: List[Dict] = []
        lifted_tools: List[Dict] = []

        def _flatten_content(c) -> str:
            """Turn Codex/OpenAI Responses content parts into a plain string."""
            if c is None:
                return ""
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                parts = []
                for block in c:
                    if isinstance(block, str):
                        parts.append(block)
                    elif isinstance(block, dict):
                        t = (
                            block.get("text")
                            or block.get("content")
                            or block.get("transcript")
                            or ""
                        )
                        if isinstance(t, list):
                            t = _flatten_content(t)
                        if t:
                            parts.append(str(t))
                return "\n".join(parts)
            if isinstance(c, dict):
                return _flatten_content(c.get("text") or c.get("content") or "")
            return str(c)

        def _args_to_str(a) -> str:
            if a is None:
                return "{}"
            if isinstance(a, str):
                return a
            try:
                from .utils import dumps as _d
                raw = _d(a)
                return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            except Exception:
                import json as _j
                return _j.dumps(a)

        def _coerce_tool(t: dict) -> dict:
            """Normalize a Codex/Responses tool def into OpenAI function tool shape."""
            if not isinstance(t, dict):
                return t

            def _fill_empty_params(name: str, params: dict) -> dict:
                if not isinstance(params, dict):
                    params = {"type": "object", "properties": {}}
                props = params.get("properties")
                if props:
                    return params
                if name == "exec":
                    return {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": "Raw JavaScript source for the V8 isolate. Use await tools.exec_command({cmd: '...'}) for shell.",
                            }
                        },
                        "required": ["code"],
                    }
                return {
                    "type": "object",
                    "properties": {
                        "input": {"type": "string", "description": "Tool input"},
                    },
                }

            if t.get("type") == "function" and isinstance(t.get("function"), dict):
                fn0 = dict(t["function"])
                name = fn0.get("name") or ""
                params = fn0.get("parameters") or {"type": "object", "properties": {}}
                fn0["parameters"] = _fill_empty_params(name, params)
                return {"type": "function", "function": fn0}

            name = t.get("name") or (t.get("function") or {}).get("name") or ""
            desc = t.get("description") or (t.get("function") or {}).get("description")
            params = (
                t.get("parameters")
                or t.get("input_schema")
                or (t.get("function") or {}).get("parameters")
                or {"type": "object", "properties": {}}
            )
            params = _fill_empty_params(name, params)
            fn: Dict[str, Any] = {"name": name, "parameters": params}
            if desc is not None:
                fn["description"] = desc
            if "strict" in t:
                fn["strict"] = t["strict"]
            return {"type": "function", "function": fn}


        if isinstance(inp, str):
            messages_list = [{"role": "user", "content": inp}]
        elif isinstance(inp, list):
            pending_tcs: List[Dict] = []

            def _flush_tcs():
                nonlocal pending_tcs
                if not pending_tcs:
                    return
                messages_list.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": pending_tcs,
                })
                pending_tcs = []

            for item in inp:
                if isinstance(item, str):
                    _flush_tcs()
                    messages_list.append({"role": "user", "content": item})
                    continue
                if not isinstance(item, dict):
                    continue
                itype = (item.get("type") or "").strip()
                role = item.get("role") or "user"

                # Codex ships tools inside input, not top-level "tools"
                if itype in ("additional_tools", "tools"):
                    raw_tools = (
                        item.get("tools")
                        or item.get("additional_tools")
                        or item.get("content")
                        or []
                    )
                    if isinstance(raw_tools, dict):
                        raw_tools = [raw_tools]
                    if isinstance(raw_tools, list):
                        for t in raw_tools:
                            if isinstance(t, dict):
                                lifted_tools.append(_coerce_tool(t))
                    log.info(
                        "req=%s lifted %d tools from input type=%s names=%s",
                        rid,
                        len(lifted_tools),
                        itype,
                        [
                            (x.get("function") or {}).get("name")
                            for x in lifted_tools[:12]
                        ],
                    )
                    try:
                        import json as _j
                        log.info(
                            "req=%s tool_schemas %s",
                            rid,
                            _j.dumps(lifted_tools, default=str)[:2500],
                        )
                    except Exception:
                        pass
                    continue

                if itype == "function_call":
                    call_id = item.get("call_id") or item.get("id") or f"call_{len(pending_tcs)}"
                    name = item.get("name") or ""
                    args = _args_to_str(item.get("arguments"))
                    pending_tcs.append({
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": args},
                    })
                    continue

                if itype in ("function_call_output", "tool_result"):
                    _flush_tcs()
                    content = _flatten_content(
                        item.get("output") or item.get("content") or item.get("text")
                    )
                    messages_list.append({
                        "role": "tool",
                        "tool_call_id": item.get("call_id") or item.get("id") or "",
                        "content": content if content is not None else "",
                    })
                    continue

                if itype in ("message", "input_message", ""):
                    _flush_tcs()
                    content = _flatten_content(
                        item.get("content") if "content" in item else item.get("text")
                    )
                    if content or role in ("user", "assistant", "system", "developer"):
                        messages_list.append({"role": role, "content": content})
                    continue

                if itype in ("input_text", "output_text", "text"):
                    _flush_tcs()
                    content = _flatten_content(item.get("text") or item.get("content"))
                    if content:
                        messages_list.append({"role": "user", "content": content})
                    continue

                if itype in ("reasoning", "summary", "refusal"):
                    _flush_tcs()
                    content = _flatten_content(
                        item.get("summary")
                        or item.get("content")
                        or item.get("text")
                        or item.get("refusal")
                    )
                    if content:
                        messages_list.append({
                            "role": "assistant",
                            "content": f"[{itype}] {content}",
                        })
                    continue

                _flush_tcs()
                content = _flatten_content(
                    item.get("content") or item.get("text") or item.get("output")
                    or item.get("arguments")
                )
                if content:
                    messages_list.append({"role": role if role else "user", "content": content})

            _flush_tcs()
        body["messages"] = messages_list

        # Merge lifted tools into body (Codex path has tools_count=0 otherwise)
        if lifted_tools:
            existing = body.get("tools") or []
            if not isinstance(existing, list):
                existing = []
            # de-dupe by function name
            seen = set()
            merged = []
            for t in existing + lifted_tools:
                if not isinstance(t, dict):
                    continue
                n = (t.get("function") or t).get("name") if isinstance(t.get("function") or t, dict) else None
                key = n or id(t)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(t)
            body["tools"] = merged
            # Free models frequently ignore tools and return empty stop.
            # Prefer "required" so the model is forced to emit at least one tool call.
            if "tool_choice" not in body or body.get("tool_choice") in (None, "auto", "none"):
                body["tool_choice"] = "required"

    if "instructions" in body and "messages" in body:
        instr = body.pop("instructions")
        body["messages"] = [
            {"role": "system", "content": instr}
        ] + body["messages"]

    body = prepare_chat_body(body)
    stream = bool(body.get("stream", False))
    payload = dumps(body)

    # --- Payload logging for inspection ---
    _payload_path = _dump_payload(rid, "_responses", body)
    # --- end payload logging ---

    tr = pl.trace(rid)  # logging-only lifecycle trace
    tr.start("openai", body.get("model"), "responses→chat", stream, payload_path=_payload_path)

    if not stream:
        # Non-streaming: forward to chat/completions and translate response
        resp = await proxy.forward(
            "POST",
            get_chat_url(),
            body=payload,
            stream=False,
            request_id=rid,
        )
        if isinstance(resp, Response):
            return _promote_text_tools_in_chat_response(resp)
        return resp

    # Streaming: we need to translate Chat Completions SSE → Responses API SSE
    from fastapi.responses import StreamingResponse


    async def translate_stream() -> AsyncIterator[bytes]:
        # Use proxy's internal client to get raw SSE stream
        assert proxy.client is not None
        key, key_idx, is_healthy = proxy.pool.next_key()
        headers = proxy._headers(key)
        t0 = time.perf_counter()

        def _evt(event: str, data: dict) -> bytes:
            raw = dumps(data)
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            return f"event: {event}\ndata: {raw}\n\n".encode("utf-8")

        # Fast-fail when all keys are cooling/suspended
        if not is_healthy:
            s = proxy.pool.stats()
            log.warning(
                "req=%s all keys unhealthy (healthy=%d cooling=%d suspended=%d) — fast-failing with 503",
                rid, s["healthy"], s["cooling"], s["suspended"],
            )
            yield _evt("response.failed", {
                "type": "response.failed",
                "response": {
                    "id": rid,
                    "error": {
                        "message": "All upstream API keys are temporarily unavailable (cooldown/suspended). Try again shortly.",
                        "type": "proxy_error",
                    },
                },
            })
            return

        item_id = f"{rid}-msg"
        content_index = 0
        fc_item_id = f"{rid}-fc"
        fc_index = 0

        # 1. response.created
        yield _evt("response.created", {
            "type": "response.created",
            "response": {
                "id": rid,
                "object": "response",
                "status": "in_progress",
                "output": [],
            },
        })

        # 2. output item + content part scaffolding (required by Codex)
        yield _evt("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": item_id,
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
        })
        yield _evt("response.content_part.added", {
            "type": "response.content_part.added",
            "output_index": 0,
            "content_index": content_index,
            "part": {"type": "output_text", "text": ""},
        })

        full_text: list[str] = []
        # Accumulate tool call fragments: index -> {id, name, arguments}
        tool_acc: dict = {}
        saw_tool_call = False

        frame_iter_or_resp = await proxy.iter_upstream_sse(
            "POST", get_chat_url(), headers, payload, key_idx, rid
        )

        if isinstance(frame_iter_or_resp, Response):
            # 429: rate limited — don't fail to client, send soft message
            if frame_iter_or_resp.status_code == 429:
                log.warning("req=%s 429 rate-limit, key marked for cooldown", rid)
                yield _evt("response.output_text.done", {
                    "type": "response.output_text.done",
                    "response_id": rid,
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": content_index,
                    "text": "[Rate limited - retrying with fresh key]",
                })
                yield _evt("response.completed", {
                    "type": "response.completed",
                    "response": {
                        "id": rid,
                        "object": "response",
                        "status": "completed",
                        "output": [{
                            "type": "message",
                            "id": item_id,
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "[Rate limited - retrying with fresh key]"}],
                        }],
                    },
                })
                return
            yield _evt("response.failed", {
                "type": "response.failed",
                "response": {
                    "id": rid,
                    "error": {
                        "message": f"upstream error: {frame_iter_or_resp.status_code}",
                        "type": "proxy_error",
                    },
                },
            })
            return

        async for frame in frame_iter_or_resp:
            frame = frame.strip()
            if not frame:
                continue
            if frame == b": keepalive\n\n":
                continue
            data_line = None
            for line in frame.split(b"\n"):
                if line.startswith(b"data:"):
                    data_line = line[5:].strip()
                    break
            if data_line is None:
                continue
            if data_line == b"[DONE]":
                break
            try:
                chunk = loads(data_line)
            except Exception:
                continue

            # Mid-stream provider error — log but don't fail to Codex
            if isinstance(chunk, dict) and (
                chunk.get("error")
                or (chunk.get("type") == "error")
            ):
                error_msg = str(chunk.get("error") or chunk)
                log.warning("req=%s mid-stream error: %s", rid, error_msg[:200])
                break

            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            # Visible text only — do not surface reasoning/thinking to Codex
            content = delta.get("content")
            finish_reason = choice.get("finish_reason")

            # Strip thinking/reasoning/internal monologue — never emit to Codex
            reasoning = delta.get("reasoning") or delta.get("reasoning_content") or delta.get("thinking")
            if reasoning:
                log.debug("req=%s filtered reasoning: %s...", rid, str(reasoning)[:80])

            # ---- text ----
            if content:
                full_text.append(content)
                yield _evt("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "response_id": rid,
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": content_index,
                    "delta": content,
                })

            # ---- tool_calls (chat completions streaming shape) ----
            tcs = delta.get("tool_calls")
            if tcs:
                saw_tool_call = True
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    idx = tc.get("index", 0)
                    fn = tc.get("function") or {}
                    fn_name = fn.get("name", "")
                    if idx not in tool_acc:
                        tool_acc[idx] = {
                            "id": tc.get("id") or f"call_{rid}_{idx}",
                            "name": fn_name,
                            "arguments": "",
                            "_announced": True,  # Native tool_calls
                        }
                        # announce function_call item — include the name
                        # from the first chunk (OpenAI always sends
                        # id + name + empty arguments in the first delta)
                        yield _evt("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": 1 + idx,
                            "item": {
                                "type": "function_call",
                                "id": tool_acc[idx]["id"],
                                "call_id": tool_acc[idx]["id"],
                                "name": fn_name,
                                "arguments": "",
                                "status": "in_progress",
                            },
                        })
                    entry = tool_acc[idx]
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    if fn_name and not entry["name"]:
                        entry["name"] = fn_name
                    if fn.get("arguments"):
                        entry["arguments"] += fn["arguments"]
                        yield _evt("response.function_call_arguments.delta", {
                            "type": "response.function_call_arguments.delta",
                            "response_id": rid,
                            "item_id": entry["id"],
                            "output_index": 1 + idx,
                            "delta": fn["arguments"],
                        })

            if finish_reason:
                log.info("req=%s finish_reason=%s", rid, finish_reason)
                break

        text_out = "".join(full_text)
        log.info(
            "req=%s stream_done text_len=%d tool_acc=%d preview=%r",
            rid,
            len(text_out),
            len(tool_acc),
            text_out[:300],
        )
        for _idx, _ent in sorted(tool_acc.items()):
            log.info(
                "req=%s tool_call[%s] name=%r args=%r",
                rid, _idx, _ent.get("name"), (_ent.get("arguments") or "")[:300],
            )

        # Free models often emit tool use as JSON text — promote to real function_calls.
        # Skip when native tool_calls were already streamed (saw_tool_call) to avoid
        # duplicates and false-positive extraction from model prose.
        if not saw_tool_call:
            text_out, text_tools = _extract_text_tool_calls(text_out)
        else:
            text_tools = []

        # Empty reply with no tools — free model stalled.
        # One transparent retry with a fresh key before giving up.
        if not text_out.strip() and not tool_acc and not text_tools:
            log.warning("req=%s empty upstream reply — attempting one transparent retry", rid)
            try:
                key2, key_idx2, _ = proxy.pool.next_key()
                headers2 = proxy._headers(key2)
                await proxy.pool.acquire(key_idx2)
                req2 = proxy.client.build_request(
                    "POST", get_chat_url(), headers=headers2, content=payload
                )
                upstream2 = await proxy.client.send(req2, stream=True)
                if upstream2.status_code < 400:
                    full_text2: list[str] = []
                    tool_acc2: dict = {}
                    buf2 = b""
                    async for raw2 in upstream2.aiter_raw():
                        if not raw2:
                            continue
                        buf2 += raw2
                        while b"\n\n" in buf2:
                            frame2, buf2 = buf2.split(b"\n\n", 1)
                            data_line2 = None
                            for line2 in frame2.split(b"\n"):
                                if line2.startswith(b"data:"):
                                    data_line2 = line2[5:].strip()
                                    break
                            if data_line2 is None or data_line2 == b"[DONE]":
                                continue
                            try:
                                chunk2 = loads(data_line2)
                            except Exception:
                                continue
                            choices2 = chunk2.get("choices") or []
                            if not choices2:
                                continue
                            delta2 = (choices2[0].get("delta") or {})
                            c2 = delta2.get("content")
                            if c2:
                                full_text2.append(c2)
                            tcs2 = delta2.get("tool_calls")
                            if tcs2:
                                for tc2 in tcs2:
                                    if not isinstance(tc2, dict):
                                        continue
                                    idx2 = tc2.get("index", 0)
                                    if idx2 not in tool_acc2:
                                        tool_acc2[idx2] = {
                                            "id": tc2.get("id") or f"call_{rid}_r_{idx2}",
                                            "name": "",
                                            "arguments": "",
                                            "_announced": True,
                                        }
                                    entry2 = tool_acc2[idx2]
                                    if tc2.get("id"):
                                        entry2["id"] = tc2["id"]
                                    fn2 = tc2.get("function") or {}
                                    if fn2.get("name"):
                                        entry2["name"] = fn2["name"]
                                    if fn2.get("arguments"):
                                        entry2["arguments"] += fn2["arguments"]
                    await upstream2.aclose()
                    await proxy.pool.mark_success(key_idx2, 0)
                    text_out = "".join(full_text2)
                    tool_acc.update(tool_acc2)
                    text_out, text_tools = _extract_text_tool_calls(text_out)
                    log.info(
                        "req=%s retry result text_len=%d tool_acc=%d",
                        rid, len(text_out), len(tool_acc),
                    )
                else:
                    await upstream2.aread()
                    await upstream2.aclose()
                    await proxy.pool.mark_error(key_idx2, upstream2.status_code)
                await proxy.pool.release(key_idx2)
            except Exception as retry_e:
                log.warning("req=%s empty-reply retry failed: %s", rid, retry_e)

            if not text_out.strip() and not tool_acc and not text_tools:
                text_out = (
                    "(model returned empty — free provider often drops tool turns. "
                    "Retry the prompt, or switch model.)"
                )
                log.warning("req=%s empty upstream reply — injected fallback text", rid)
        for i, t in enumerate(text_tools):
            idx = len(tool_acc)
            # avoid colliding with native tool_acc indices
            while idx in tool_acc:
                idx += 1
            call_id = f"call_{rid}_txt_{i}"
            tool_acc[idx] = {
                "id": call_id,
                "name": t["name"],
                "arguments": t["arguments"],
                "_announced": False,  # Text-promoted, needs announcement
            }
            log.info(
                "req=%s promoted text tool_call name=%s args=%s",
                rid,
                t["name"],
                t["arguments"][:120],
            )


        # ------------------------------------------------------------------
        # Anti-loop hack (symptom-patch, not protocol design):
        # Free-tier models (e.g. Nemotron) sometimes refuse to emit plain text
        # when tool_choice=required is set. Instead they wrap the intended text
        # inside a fake exec tool call:  exec({"code": "text('hey')"}).
        # If we forward that as a real tool call, the agent tries to execute
        # text('hey') as shell code → no-op → model repeats → infinite loop.
        # This hack detects that pattern, converts it back to plain assistant
        # text, and drops the tool call. Fix the root cause by switching the
        # model or removing tool_choice=required when plain text is desired.
        # ------------------------------------------------------------------
        import re as _re_loop
        _text_only = _re_loop.compile(
            r"""^\s*text\s*\(\s*(['"])(.*?)\1\s*\)\s*$""",
            _re_loop.DOTALL,
        )
        _to_drop = []
        for _idx, _ent in list(tool_acc.items()):
            if (_ent.get("name") or "").lower() != "exec":
                continue
            args_raw = _ent.get("arguments") or ""
            try:
                import json as _j
                args_obj = _j.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except Exception:
                args_obj = {}
            code = ""
            if isinstance(args_obj, dict):
                code = args_obj.get("code") or args_obj.get("cmd") or ""
            m = _text_only.match(code.strip()) if isinstance(code, str) else None
            if m:
                extracted = m.group(2)
                if extracted:
                    if text_out:
                        text_out = text_out + "\n" + extracted
                    else:
                        text_out = extracted
                    _to_drop.append(_idx)
                    log.info(
                        "req=%s collapsed exec(text(...)) -> assistant text %r",
                        rid, extracted[:120],
                    )
        for _idx in _to_drop:
            tool_acc.pop(_idx, None)


        # Announce text-promoted function_call items BEFORE closing message
        for idx in sorted(tool_acc.keys()):
            entry = tool_acc[idx]
            if not entry.get("_announced"):
                log.info("req=%s announcing text-promoted tool name=%s id=%s", rid, entry["name"], entry["id"])
                yield _evt("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": 1 + idx,
                    "item": {
                        "type": "function_call",
                        "id": entry["id"],
                        "call_id": entry["id"],
                        "name": entry["name"],
                        "arguments": entry["arguments"],
                        "status": "completed",
                    },
                })

        # close text content part + message item
        yield _evt("response.output_text.done", {
            "type": "response.output_text.done",
            "response_id": rid,
            "item_id": item_id,
            "output_index": 0,
            "content_index": content_index,
            "text": text_out,
        })
        yield _evt("response.content_part.done", {
            "type": "response.content_part.done",
            "output_index": 0,
            "content_index": content_index,
            "part": {"type": "output_text", "text": text_out},
        })
        yield _evt("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": item_id,
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text_out}],
            },
        })

        # close any function_call items
        output_items = [{
            "type": "message",
            "id": item_id,
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text_out}],
        }]
        for idx in sorted(tool_acc.keys()):
            entry = tool_acc[idx]
            yield _evt("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "response_id": rid,
                "item_id": entry["id"],
                "output_index": 1 + idx,
                "arguments": entry["arguments"],
            })
            fc_item = {
                "type": "function_call",
                "id": entry["id"],
                "call_id": entry["id"],
                "name": entry["name"],
                "arguments": entry["arguments"],
                "status": "completed",
            }
            yield _evt("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": 1 + idx,
                "item": fc_item,
            })
            output_items.append(fc_item)

        # final completed event
        yield _evt("response.completed", {
            "type": "response.completed",
            "response": {
                "id": rid,
                "object": "response",
                "status": "completed",
                "output": output_items,
            },
        })

    return StreamingResponse(
        translate_stream(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache, no-transform",
            "x-accel-buffering": "no",
            "connection": "keep-alive",
            "x-request-id": rid,
        },
    )


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
    except Exception:
        return ""
    return path


def _force_default_model_in_ws_message(msg: str) -> str:
    """Apply the provider's hardcoded default model to outgoing WS messages.

    The HTTP paths already override the model via prepare_chat_body /
    prepare_messages_body / proxy._enforce_default_model. The WebSocket
    pass-through has no such normalisation layer, so we apply the same
    rule here: when get_force_default_model() is True, any outgoing JSON
    message carrying a `model` field has that field replaced with
    `get_default_model()` for the active provider. Returns the message
    unchanged when it is not a JSON object or has no model field.
    """
    if not get_force_default_model():
        return msg
    try:
        parsed = loads(msg)
    except Exception:
        return msg
    if not isinstance(parsed, dict) or "model" not in parsed:
        return msg
    parsed["model"] = get_default_model()
    raw = dumps(parsed)
    return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw


@router.websocket("/v1/responses")
async def responses_websocket(ws: WebSocket) -> None:
    """
    Bidirectional WebSocket proxy for the OpenAI Responses API.

    Responsibilities:
      - Accept the downstream client connection.
      - Connect to the upstream provider with key injection.
      - Force the configured provider model on outgoing JSON messages.
      - Keep both directions alive independently.
      - Detect when either side terminates and immediately tear down the
        opposite direction instead of leaving a dangling coroutine behind.
      - Log connection lifecycle events and termination reasons.
      - Correctly release key-pool in-flight accounting exactly once.
      - Never report a prematurely terminated stream as a successful request.
    """
    import asyncio

    await ws.accept()

    assert proxy is not None

    rid = ws_request_id(ws)
    upstream = None
    key_idx = -1
    completed_cleanly = False
    termination_reason = "unknown"

    log.info("req=%s ws accepted", rid)

    # ------------------------------------------------------------------
    # Connect to upstream
    # ------------------------------------------------------------------
    for attempt in range(MAX_RETRIES + 1):
        key, ki, is_healthy = proxy.pool.next_key()
        key_idx = ki

        if not is_healthy and attempt == 0:
            stats = proxy.pool.stats()

            log.warning(
                "req=%s ws unavailable: all keys unhealthy "
                "(healthy=%d cooling=%d suspended=%d)",
                rid,
                stats["healthy"],
                stats["cooling"],
                stats["suspended"],
            )

            await ws.close(
                code=1011,
                reason="All upstream API keys are temporarily unavailable",
            )
            return

        headers = proxy._headers(key)

        try:
            await proxy.pool.acquire(key_idx)

            upstream_url = (
                OPENROUTER_RESPONSES
                .replace("https://", "wss://")
                .replace("http://", "ws://")
            )

            log.info(
                "req=%s ws connecting upstream attempt=%d key_idx=%d",
                rid,
                attempt + 1,
                key_idx,
            )

            upstream = await proxy.client.ws_connect(
                upstream_url,
                headers=headers,
            )

            log.info(
                "req=%s ws upstream connected key_idx=%d",
                rid,
                key_idx,
            )

            break

        except Exception as exc:
            log.warning(
                "req=%s ws upstream connect failed "
                "attempt=%d key_idx=%d error=%s",
                rid,
                attempt + 1,
                key_idx,
                exc,
            )

            try:
                await proxy.pool.mark_error(key_idx, 599)
            except Exception:
                pass

            try:
                await proxy.pool.release(key_idx)
            except Exception:
                pass

            if attempt >= MAX_RETRIES:
                await ws.close(
                    code=1011,
                    reason="Failed to connect to upstream after retries",
                )
                return

            await asyncio.sleep(0.5 * (2 ** attempt))

    if upstream is None:
        await ws.close(
            code=1011,
            reason="Upstream connection failed",
        )
        return

    # ------------------------------------------------------------------
    # Client -> upstream
    # ------------------------------------------------------------------
    async def client_to_upstream() -> str:
        try:
            while True:
                message = await ws.receive()
                message_type = message.get("type")

                if message_type == "websocket.disconnect":
                    code = message.get("code", 1000)

                    log.info(
                        "req=%s ws client disconnected code=%s",
                        rid,
                        code,
                    )

                    return "client_disconnect"

                if message_type != "websocket.receive":
                    continue

                text = message.get("text")

                if text is None:
                    data = message.get("bytes")

                    if data is None:
                        continue

                    try:
                        await upstream.send_bytes(data)
                    except Exception as exc:
                        log.warning(
                            "req=%s ws client->upstream send bytes failed: %s",
                            rid,
                            exc,
                        )
                        return "upstream_send_error"

                    continue

                if not text:
                    continue

                # Authoritative model override at the upstream boundary.
                text = _force_default_model_in_ws_message(text)

                await upstream.send_text(text)

        except WebSocketDisconnect as exc:
            log.info(
                "req=%s ws client disconnected exception code=%s",
                rid,
                getattr(exc, "code", None),
            )
            return "client_disconnect"

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            log.warning(
                "req=%s ws client->upstream terminated: %s",
                rid,
                exc,
            )
            return "client_to_upstream_error"

    # ------------------------------------------------------------------
    # Upstream -> client
    # ------------------------------------------------------------------
    async def upstream_to_client() -> str:
        try:
            while True:
                message = await upstream.receive()
                message_type = message.get("type")

                if message_type == "websocket.disconnect":
                    code = message.get("code", 1000)

                    log.warning(
                        "req=%s ws upstream disconnected code=%s",
                        rid,
                        code,
                    )

                    return "upstream_disconnect"

                if message_type != "websocket.receive":
                    continue

                text = message.get("text")

                if text is not None:
                    if text:
                        await ws.send_text(text)
                    continue

                data = message.get("bytes")

                if data is not None:
                    await ws.send_bytes(data)

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            log.warning(
                "req=%s ws upstream->client terminated: %s",
                rid,
                exc,
            )
            return "upstream_to_client_error"

    # ------------------------------------------------------------------
    # Supervise both directions.
    #
    # IMPORTANT:
    # asyncio.gather() is deliberately NOT used here. If one direction
    # terminates, the other direction must be cancelled immediately.
    # ------------------------------------------------------------------
    client_task = asyncio.create_task(
        client_to_upstream(),
        name=f"atlas-ws-client-to-upstream-{rid}",
    )

    upstream_task = asyncio.create_task(
        upstream_to_client(),
        name=f"atlas-ws-upstream-to-client-{rid}",
    )

    try:
        done, pending = await asyncio.wait(
            {client_task, upstream_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in done:
            try:
                result = task.result()
                if result:
                    termination_reason = result
            except asyncio.CancelledError:
                termination_reason = "task_cancelled"
            except Exception as exc:
                termination_reason = "task_exception"

                log.warning(
                    "req=%s ws completed task raised: %s",
                    rid,
                    exc,
                )

        # The opposite direction must not remain blocked indefinitely.
        for task in pending:
            task.cancel()

        if pending:
            await asyncio.gather(
                *pending,
                return_exceptions=True,
            )

        # If both directions completed at effectively the same time,
        # collect the second result as well.
        if len(done) == 2:
            reasons = []

            for task in done:
                try:
                    result = task.result()
                    if result:
                        reasons.append(result)
                except asyncio.CancelledError:
                    reasons.append("task_cancelled")
                except Exception:
                    reasons.append("task_exception")

            if reasons:
                termination_reason = ",".join(reasons)

        # A client disconnect is a normal end of the WS session.
        # Anything else means the session terminated unexpectedly.
        completed_cleanly = termination_reason == "client_disconnect"

        log.info(
            "req=%s ws session ended reason=%s "
            "clean=%s pending_cancelled=%d",
            rid,
            termination_reason,
            completed_cleanly,
            len(pending),
        )

    except asyncio.CancelledError:
        log.warning(
            "req=%s ws handler cancelled — terminating upstream session",
            rid,
        )

        client_task.cancel()
        upstream_task.cancel()

        await asyncio.gather(
            client_task,
            upstream_task,
            return_exceptions=True,
        )

        raise

    except Exception as exc:
        log.exception(
            "req=%s ws supervisor failure: %s",
            rid,
            exc,
        )

    finally:
        # ------------------------------------------------------------------
        # Cancel anything still alive.
        # ------------------------------------------------------------------
        for task in (client_task, upstream_task):
            if not task.done():
                task.cancel()

        await asyncio.gather(
            client_task,
            upstream_task,
            return_exceptions=True,
        )

        # ------------------------------------------------------------------
        # Close upstream exactly once.
        # ------------------------------------------------------------------
        if upstream is not None:
            try:
                await upstream.aclose()
            except Exception as exc:
                log.debug(
                    "req=%s ws upstream close error: %s",
                    rid,
                    exc,
                )

        # ------------------------------------------------------------------
        # Do not mark a prematurely terminated stream as successful.
        # Guard key_idx >= 0: if next_key() raised before assignment (empty
        # pool), there is no acquired slot to mark or release.
        # ------------------------------------------------------------------
        if key_idx >= 0:
            try:
                if completed_cleanly:
                    await proxy.pool.mark_success(
                        key_idx,
                        0,
                    )
                else:
                    await proxy.pool.mark_error(
                        key_idx,
                        499,
                    )
            except Exception:
                pass

        # ------------------------------------------------------------------
        # Release the in-flight key slot exactly once.
        # ------------------------------------------------------------------
        if key_idx >= 0:
            try:
                await proxy.pool.release(key_idx)
            except Exception:
                pass

        log.info(
            "req=%s ws cleanup complete key_idx=%d reason=%s",
            rid,
            key_idx,
            termination_reason,
        )

        try:
            await ws.close()
        except Exception:
            pass
