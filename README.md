# RZ AI

A small FastAPI service for a local personal AI assistant, with chat requests routed through OpenRouter.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `RZ_API_KEY` and `OPENROUTER_API_KEY`. Keep `.env` private; never commit it.

## Run locally

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8788
```

- Health: `http://127.0.0.1:8788/health`
- Interactive API docs: `http://127.0.0.1:8788/docs`
- Chat: `POST http://127.0.0.1:8788/v1/chat` with JSON `{"message":"Hello"}` and an `X-Api-Key` header matching `RZ_API_KEY`.

The chat request defaults to `openai/gpt-oss-20b` and sends OpenRouter provider preferences with Zero Data Retention enabled and provider fallbacks disabled. Provider-side data handling remains subject to OpenRouter's terms and the selected provider's policies.

The service binds to loopback in the example command so it is not exposed to the network by default.
