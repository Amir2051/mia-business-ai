import hmac
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

load_dotenv()

app = FastAPI(
    title="RZ AI",
    description="Local personal AI assistant API",
    version="0.2.0",
)

API_KEY = os.getenv("RZ_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL", "openai/gpt-oss-20b"
).strip()


def require_api_key(x_api_key: str = Header(default="")):
    if not API_KEY or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!doctype html>
    <html>
      <head><meta charset="utf-8"><title>RZ AI</title></head>
      <body>
        <h1>RZ AI</h1>
        <p>Local AI assistant API</p>
        <p><a href="/health">Health</a></p>
        <p><a href="/docs">API documentation</a></p>
      </body>
    </html>
    """


@app.get("/health")
def health():
    return {"status": "ok", "service": "rz-ai-agent"}


@app.post("/v1/chat")
def chat(request: ChatRequest, _auth=Depends(require_api_key)):
    if not OPENROUTER_API_KEY:
        raise HTTPException(
            status_code=503, detail="AI provider is not configured"
        )

    payload = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": request.message}],
        "provider": {"zdr": True, "allow_fallbacks": False},
    }).encode("utf-8")

    outbound = Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(outbound, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))

    except HTTPError as exc:
        if exc.code in (401, 403):
            raise HTTPException(
                status_code=502,
                detail="OpenRouter rejected the provider credentials",
            ) from None
        if exc.code == 429:
            raise HTTPException(
                status_code=503,
                detail="OpenRouter rate limit or quota reached",
            ) from None
        raise HTTPException(
            status_code=502,
            detail=f"OpenRouter request failed (HTTP {exc.code})",
        ) from None

    except (TimeoutError, URLError):
        raise HTTPException(
            status_code=504, detail="OpenRouter request timed out or failed"
        ) from None

    except (ValueError, KeyError, TypeError):
        raise HTTPException(
            status_code=502, detail="Invalid response from OpenRouter"
        ) from None

    try:
        reply = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise HTTPException(
            status_code=502, detail="OpenRouter returned no assistant response"
        ) from None

    if not isinstance(reply, str):
        raise HTTPException(
            status_code=502, detail="Unexpected assistant response format"
        )

    return {
        "status": "ok",
        "model": result.get("model", OPENROUTER_MODEL),
        "reply": reply,
    }
