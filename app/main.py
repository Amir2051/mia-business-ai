import hmac
import json
import os
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request as URLRequest, urlopen

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pathlib import Path
from pydantic import BaseModel, Field

load_dotenv()

app = FastAPI(
    title="RZ AI",
    description="Local personal AI assistant API (OpenRouter primary + Hermes default fallback)",
    version="0.3.0",
)

# ---------------------------------------------------------------- server auth
DATABASE_AUTH_ENABLED = bool(os.getenv("DATABASE_URL", "").strip())
app.state.auth_attempts = {}
if DATABASE_AUTH_ENABLED:
    from app.auth import get_current_user as _get_database_user
    from app.auth import router as auth_router
    app.include_router(auth_router)

API_KEY = os.getenv("RZ_API_KEY", "").strip()
if not API_KEY and not DATABASE_AUTH_ENABLED:
    raise RuntimeError(
        "Set RZ_API_KEY for local mode or DATABASE_URL for account-auth mode."
    )


def require_api_key(x_api_key: str = Header(default="")):
    if not API_KEY or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_app_user(request: Request, x_api_key: str = Header(default="")):
    """Use persistent accounts in cloud mode and preserve API-key auth locally."""
    if DATABASE_AUTH_ENABLED:
        return _get_database_user(request)
    require_api_key(x_api_key)
    return {"id": "local", "email": "local@rz-agent"}


@app.get("/v1/auth/mode")
def auth_mode():
    return {"mode": "account" if DATABASE_AUTH_ENABLED else "api_key"}


# ---------------------------------------------------------------- OpenRouter (primary)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-20b").strip()
OPENROUTER_CODING_MODEL = os.getenv("OPENROUTER_CODING_MODEL", "").strip()
OPENROUTER_ANALYSIS_MODEL = os.getenv("OPENROUTER_ANALYSIS_MODEL", "").strip()


def _select_model_for_task(message: str, requested_model: str | None = None) -> str:
    """Choose an optional task-specific model; never override an explicit user choice."""
    if requested_model and requested_model.strip():
        return requested_model.strip()

    text = message.lower()
    coding_terms = (
        "debug", "debugging", "stack trace", "traceback", "write code",
        "fix this code", "programming", "python", "javascript", "typescript",
        "sql query", "unit test", "refactor", "compile error", "deployment error",
    )
    analysis_terms = (
        "analyze", "analysis", "compare", "evaluate", "investigate",
        "research", "pros and cons", "strategy", "root cause", "plan architecture",
    )
    if OPENROUTER_CODING_MODEL and any(term in text for term in coding_terms):
        return OPENROUTER_CODING_MODEL
    if OPENROUTER_ANALYSIS_MODEL and any(term in text for term in analysis_terms):
        return OPENROUTER_ANALYSIS_MODEL
    return OPENROUTER_MODEL
OPENROUTER_ENDPOINT = os.getenv(
    "OPENROUTER_ENDPOINT", "https://openrouter.ai/api/v1/chat/completions"
).strip()

def _openrouter_chat_completions_url(endpoint: str) -> str:
    """Accept either an OpenRouter API base URL or the full chat endpoint."""
    endpoint = endpoint.strip().rstrip("/")
    if endpoint.endswith("/chat/completions"):
        return endpoint
    if endpoint.endswith("/v1"):
        return f"{endpoint}/chat/completions"
    if endpoint.endswith("/api"):
        return f"{endpoint}/v1/chat/completions"
    if "://" in endpoint and endpoint.split("://", 1)[1].split("/", 1)[-1] == endpoint.split("://", 1)[1]:
        return f"{endpoint}/api/v1/chat/completions"
    return endpoint

# ---------------------------------------------------------------- Hermes default (fallback)
# The Hermes default inference API (Nous Research). Used when OpenRouter cannot
# authenticate or fails so the assistant keeps replying.
HERMES_BASE_URL = os.getenv(
    "HERMES_BASE_URL", "https://inference-api.nousresearch.com/v1"
).strip().rstrip("/")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "").strip()
HERMES_MODEL = os.getenv("HERMES_MODEL", "upstage/solar-mini4").strip()
HERMES_ENABLED = bool(HERMES_API_KEY or os.getenv("HERMES_DEFAULT", "0").strip().lower() in {"1", "true", "yes"})

# ---------------------------------------------------------------- session store
HISTORY_LIMIT = int(os.getenv("SESSION_HISTORY_LIMIT", "12"))
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))

_store_lock = threading.Lock()
_conversations = {}  # session_id -> {"history": [...], "updated": float, "model": str}


def _is_placeholder(token: str) -> bool:
    """True when a token looks like a redacted/placeholder value rather than a real key."""
    t = token.strip()
    if not t:
        return True
    return set(t) <= {"*", "?", ".", "-"} or t.startswith("***")


# ---------------------------------------------------------------- session store

def _prune_expired_sessions_locked(now: float | None = None) -> int:
    """Remove idle in-memory sessions; caller must hold _store_lock."""
    current = time.time() if now is None else now
    expired = [
        session_id
        for session_id, conv in _conversations.items()
        if current - conv.get("updated", current) > SESSION_TTL_SECONDS
    ]
    for session_id in expired:
        _conversations.pop(session_id, None)
    return len(expired)


def _get_session(session_id: str) -> dict:
    with _store_lock:
        _prune_expired_sessions_locked()
        conv = _conversations.get(session_id)
        if conv is None:
            conv = {"history": [], "updated": time.time(), "model": OPENROUTER_MODEL}
            _conversations[session_id] = conv
        return conv


def _touch_session(session_id: str, model: str) -> dict:
    conv = _get_session(session_id)
    conv["history"] = conv["history"][-HISTORY_LIMIT:]
    conv["updated"] = time.time()
    conv["model"] = model
    with _store_lock:
        _conversations[session_id] = conv
    return conv


# ---------------------------------------------------------------- request model

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(
        default=None,
        description="Conversation id. Omit to create a new session (a random one is assigned).",
    )
    model: str | None = Field(
        default=None, description="Override the provider model for this request."
    )
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=16384)
    system: str = Field(
        default="You are a concise, helpful local assistant.",
        description="System prompt prepended to the conversation.",
    )


class ChatResponse(BaseModel):
    status: str
    session_id: str
    model: str
    provider: str
    reply: str
    history_length: int
    updated_at: str


# ---------------------------------------------------------------- providers

def _base_messages(system: str, user_content: str, session_id: str) -> list[dict]:
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    for entry in _get_session(session_id)["history"]:
        if isinstance(entry, dict) and entry.get("content") is not None:
            messages.append(
                {"role": entry.get("role", "assistant"), "content": entry["content"]}
            )
    messages.append({"role": "user", "content": user_content})
    return messages


def _call_openrouter(messages: list[dict], model: str, temperature, max_tokens) -> dict:
    payload: dict = {
        "model": model or OPENROUTER_MODEL,
        "messages": messages,
        "provider": {"zdr": True, "allow_fallbacks": False},
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    data = json.dumps(payload).encode("utf-8")
    req = URLRequest(
        _openrouter_chat_completions_url(OPENROUTER_ENDPOINT),
        data=data,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://rz-ai-agent.local",
            "X-Title": "RZ AI",
        },
        method="POST",
    )
    with urlopen(req, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def _call_hermes(messages: list[dict], model: str, temperature, max_tokens) -> dict:
    payload: dict = {
        "model": model or HERMES_MODEL,
        "messages": messages,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    data = json.dumps(payload).encode("utf-8")
    headers: dict = {"Content-Type": "application/json"}
    if HERMES_API_KEY:
        headers["Authorization"] = f"Bearer {HERMES_API_KEY}"
    else:
        headers["X-Api-Key"] = ""
    req = URLRequest(
        f"{HERMES_BASE_URL}/chat/completions",
        data=data,
        headers=headers,
        method="POST",
    )
    with urlopen(req, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def _extract_reply(result: dict) -> str:
    try:
        message = result["choices"][0]["message"]
        reply = message.get("content")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=502, detail="AI provider returned no assistant response"
        ) from exc

    if isinstance(reply, str):
        text = reply.strip()
        if text:
            return text

    elif isinstance(reply, list):
        parts = []
        for block in reply:
            if isinstance(block, str):
                if block.strip():
                    parts.append(block.strip())
            elif isinstance(block, dict) and block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())

        if parts:
            return "\\n".join(parts)

    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        return refusal.strip()

    raise HTTPException(
        status_code=502,
        detail="AI provider returned no usable text content"
    )


def _complete_message(
    system: str,
    user_content: str,
    session_id: str,
    model: str | None,
    temperature,
    max_tokens,
) -> ChatResponse:
    providers = []
    openrouter_configured = bool(
        OPENROUTER_API_KEY and not _is_placeholder(OPENROUTER_API_KEY)
    )
    if openrouter_configured:
        # Privacy guard: never fail over from OpenRouter's enforced ZDR route
        # to an external provider whose ZDR eligibility has not been verified.
        providers.append(("openrouter", _call_openrouter))
    elif HERMES_ENABLED:
        # Hermes may be used as the primary only when OpenRouter is not configured.
        providers.append(("hermes", _call_hermes))

    result = None
    provider_model_used = None
    last_error: Exception | None = None
    for name, fn in providers:
        provider_model = (
            (model or OPENROUTER_MODEL) if name == "openrouter" else HERMES_MODEL
        )
        try:
            result = fn(
                _base_messages(system, user_content, session_id),
                provider_model,
                temperature,
                max_tokens,
            )
            provider = name
            provider_model_used = provider_model
            break
        except HTTPException:
            raise
        except HTTPError as exc:
            # Log only provider/model and the provider's structured error message.
            # Never log authorization headers, prompts, or response content.
            upstream_code = None
            upstream_message = ""
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
                error_info = error_payload.get("error", {})
                if isinstance(error_info, dict):
                    upstream_code = error_info.get("code")
                    upstream_message = str(error_info.get("message", ""))[:240]
                elif isinstance(error_info, str):
                    upstream_message = error_info[:240]
            except Exception:
                pass
            provider_model = (
                model or OPENROUTER_MODEL if name == "openrouter"
                else model or HERMES_MODEL
            )
            print(
                "RZ_PROVIDER_ERROR",
                {
                    "provider": name,
                    "status": exc.code,
                    "provider_code": upstream_code,
                    "provider_message": upstream_message,
                    "model": provider_model,
                },
                flush=True,
            )
            last_error = RuntimeError(
                f"{name} HTTP {exc.code}"
                + (f" ({upstream_message})" if upstream_message else "")
            )
            continue
        except Exception as exc:  # noqa: BLE001 - fallback to next provider
            print(
                "RZ_PROVIDER_ERROR",
                {
                    "provider": name,
                    "error_type": type(exc).__name__,
                    "model": model or (OPENROUTER_MODEL if name == "openrouter" else HERMES_MODEL),
                },
                flush=True,
            )
            last_error = exc
            continue

    if result is None:
        if last_error is not None:
            detail = f"AI providers unavailable: last error {last_error!r}"
        else:
            detail = "AI providers unavailable (no usable primary or fallback configured)"
        raise HTTPException(status_code=502, detail=detail)

    try:
        reply = _extract_reply(result)
    except HTTPException:
        choices = result.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") if isinstance(choice, dict) else {}
        message = message if isinstance(message, dict) else {}
        content = message.get("content")

        print(
            "RZ_RESPONSE_DIAGNOSTIC",
            {
                "model": result.get("model"),
                "choices_count": len(choices) if isinstance(choices, list) else None,
                "finish_reason": choice.get("finish_reason") if isinstance(choice, dict) else None,
                "content_type": type(content).__name__,
                "content_length": len(content) if isinstance(content, (str, list)) else None,
                "refusal_present": bool(message.get("refusal")),
                "tool_calls_present": bool(message.get("tool_calls")),
            },
            flush=True,
        )
        raise

    history = _get_session(session_id)["history"]
    history.append({"role": "user", "content": user_content})
    history.append({"role": "assistant", "content": reply})
    _touch_session(session_id, provider_model_used or model or OPENROUTER_MODEL)
    history = _get_session(session_id)["history"]

    return ChatResponse(
        status="ok",
        session_id=session_id,
        model=provider_model_used or model or OPENROUTER_MODEL,
        provider=provider,
        reply=reply,
        history_length=len(history),
        updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


# ---------------------------------------------------------------- endpoints

@app.get("/", response_class=HTMLResponse)
def home():
    return (Path(__file__).parent / "ui.html").read_text(encoding="utf-8")


@app.get("/v1/auth/check")
def check_api_key(_auth=Depends(require_api_key)):
    """Validate the RZ API key without invoking the language model."""
    return {"status": "ok", "authenticated": True}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "rz-ai-agent",
        "providers": {
            "openrouter": bool(OPENROUTER_API_KEY and not _is_placeholder(OPENROUTER_API_KEY)),
            "hermes_default": HERMES_ENABLED,
        },
    }


@app.post("/v1/chat")
def chat(request: ChatRequest, current_user: dict = Depends(require_app_user)):
    if not request.message:
        raise HTTPException(status_code=400, detail="message is required")
    prefix = f"{current_user['id']}:"
    if request.session_id and request.session_id.startswith(prefix):
        session_id = request.session_id
    elif request.session_id and ":" in request.session_id:
        raise HTTPException(status_code=403, detail="Conversation does not belong to this account")
    else:
        session_id = prefix + (request.session_id or uuid.uuid4().hex[:12])
    selected_model = _select_model_for_task(request.message, request.model)
    return _complete_message(
        system=request.system,
        user_content=request.message,
        session_id=session_id,
        model=selected_model,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
    )


@app.post("/v1/conversations/{session_id}")
def reset_conversation(session_id: str, current_user: dict = Depends(require_app_user)):
    if not session_id.startswith(f"{current_user['id']}:"):
        raise HTTPException(status_code=404, detail="Conversation not found")
    with _store_lock:
        _conversations.pop(session_id, None)
    return {"status": "ok", "session_id": session_id, "reset": True}


@app.get("/v1/conversations")
def list_conversations(current_user: dict = Depends(require_app_user)):
    prefix = f"{current_user['id']}:"
    with _store_lock:
        _prune_expired_sessions_locked()
        ids = sorted(k for k in _conversations.keys() if k.startswith(prefix))
    return {"status": "ok", "count": len(ids), "sessions": ids}


# ---------------------------------------------------------------- Model Context Protocol (MCP)
MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_SERVER_INFO = {"name": "rz-agent", "version": "0.1.0"}

MCP_TOOLS = [
    {
        "name": "get_runtime_status",
        "description": "Get safe RZ Agent runtime, model-routing, privacy-flag, and session-limit status. Never returns credentials or conversation text.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_conversations",
        "description": "List the authenticated user's active in-memory conversation IDs. Does not return message contents.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "chat",
        "description": "Send a message to RZ Agent's configured AI model. Optionally continue one of your own conversation IDs. Provider ZDR and no-fallback settings remain enforced by the existing backend.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "minLength": 1, "maxLength": 4000},
                "session_id": {"type": "string", "description": "Optional conversation ID previously returned for your account."},
            },
            "required": ["message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "reset_conversation",
        "description": "Delete an active in-memory conversation belonging to the authenticated user. This action cannot be undone.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string", "minLength": 1}},
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
]


def _mcp_text_result(value: dict, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "structuredContent": value,
        "isError": is_error,
    }


@app.post("/mcp")
async def mcp_endpoint(payload: dict, current_user: dict = Depends(require_app_user)):
    """Minimal authenticated MCP JSON-RPC endpoint for Streamable HTTP clients."""
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return JSONResponse(status_code=400, content={
            "jsonrpc": "2.0", "id": payload.get("id") if isinstance(payload, dict) else None,
            "error": {"code": -32600, "message": "Invalid JSON-RPC request"},
        })

    method = payload.get("method")
    request_id = payload.get("id")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    # MCP notifications intentionally have no response body.
    if method == "notifications/initialized" and request_id is None:
        return Response(status_code=202)

    if method == "initialize":
        result = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": MCP_SERVER_INFO,
            "instructions": "RZ Agent tools use the authenticated user's existing RZ Agent backend. Runtime status omits secrets. Chat follows the configured provider privacy settings.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": MCP_TOOLS}
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            if name == "get_runtime_status":
                value = capabilities(current_user)
            elif name == "list_conversations":
                value = list_conversations(current_user)
            elif name == "chat":
                message = arguments.get("message")
                if not isinstance(message, str) or not message.strip() or len(message) > 4000:
                    raise ValueError("message must contain 1–4000 characters")
                prefix = f"{current_user['id']}:"
                supplied_id = arguments.get("session_id")
                if supplied_id and (not isinstance(supplied_id, str) or not supplied_id.startswith(prefix)):
                    raise ValueError("Conversation does not belong to this account")
                session_id = supplied_id or prefix + uuid.uuid4().hex[:12]
                response = _complete_message(
                    system="You are RZ Agent, a concise, helpful personal AI assistant.",
                    user_content=message,
                    session_id=session_id,
                    model=_select_model_for_task(message),
                    temperature=None,
                    max_tokens=1200,
                )
                value = response.model_dump() if hasattr(response, "model_dump") else response.dict()
            elif name == "reset_conversation":
                session_id = arguments.get("session_id")
                if not isinstance(session_id, str) or not session_id.startswith(f"{current_user['id']}:"):
                    raise ValueError("Conversation does not belong to this account")
                with _store_lock:
                    existed = _conversations.pop(session_id, None) is not None
                if not existed:
                    raise ValueError("Conversation not found or already expired")
                value = {"status": "ok", "session_id": session_id, "reset": True}
            else:
                return JSONResponse(status_code=200, content={
                    "jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": f"Unknown tool: {name}"},
                })
            result = _mcp_text_result(value)
        except (ValueError, TypeError) as exc:
            result = _mcp_text_result({"error": str(exc)}, is_error=True)
        except HTTPException as exc:
            result = _mcp_text_result({"error": exc.detail}, is_error=True)
    elif method == "resources/list":
        result = {"resources": []}
    elif method == "prompts/list":
        result = {"prompts": []}
    elif method == "notifications/cancelled" and request_id is None:
        return Response(status_code=202)
    else:
        return JSONResponse(status_code=200, content={
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })

    if request_id is None:
        return Response(status_code=202)
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


@app.get("/v1/capabilities")
def capabilities(_current_user: dict = Depends(require_app_user)):
    """Expose safe runtime capabilities without returning credentials or prompts."""
    openrouter_configured = bool(
        OPENROUTER_API_KEY and not _is_placeholder(OPENROUTER_API_KEY)
    )
    return {
        "status": "ok",
        "provider": "openrouter" if openrouter_configured else (
            "hermes" if HERMES_ENABLED else "none"
        ),
        "default_model": OPENROUTER_MODEL if openrouter_configured else (
            HERMES_MODEL if HERMES_ENABLED else None
        ),
        "task_routing": {
            "coding_model_configured": bool(OPENROUTER_CODING_MODEL),
            "analysis_model_configured": bool(OPENROUTER_ANALYSIS_MODEL),
        },
        "privacy": {
            "zdr_requested": openrouter_configured,
            "provider_fallbacks_disabled": openrouter_configured,
            "unverified_provider_fallback_disabled": openrouter_configured,
        },
        "session_history_limit": HISTORY_LIMIT,
        "session_ttl_seconds": SESSION_TTL_SECONDS,
    }
