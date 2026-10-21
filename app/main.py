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
from fastapi.responses import HTMLResponse
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
OPENROUTER_ENDPOINT = os.getenv(
    "OPENROUTER_ENDPOINT", "https://openrouter.ai/api/v1/chat/completions"
).strip()

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

def _get_session(session_id: str) -> dict:
    with _store_lock:
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
        OPENROUTER_ENDPOINT,
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
    if OPENROUTER_API_KEY and not _is_placeholder(OPENROUTER_API_KEY):
        providers.append(("openrouter", _call_openrouter))
    if HERMES_ENABLED:
        providers.append(("hermes", _call_hermes))

    result = None
    last_error: Exception | None = None
    for name, fn in providers:
        try:
            result = fn(_base_messages(system, user_content, session_id), model, temperature, max_tokens)
            provider = name
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
    _touch_session(session_id, model or OPENROUTER_MODEL)
    history = _get_session(session_id)["history"]

    return ChatResponse(
        status="ok",
        session_id=session_id,
        model=model or OPENROUTER_MODEL,
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
    return _complete_message(
        system=request.system,
        user_content=request.message,
        session_id=session_id,
        model=request.model,
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
        ids = sorted(k for k in _conversations.keys() if k.startswith(prefix))
    return {"status": "ok", "count": len(ids), "sessions": ids}
